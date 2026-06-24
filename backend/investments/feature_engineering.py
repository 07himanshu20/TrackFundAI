"""
Feature Engineering Layer — computes financial ratios, Z-scores, and trends
from raw MIS data for use in risk scoring (rule-based and XGBoost Phase 2).

v5 AI Analytics: Feature Engineering layer between raw MIS and ML model.

As a CA with 25+ years experience, these ratios follow Indian GAAP / Ind-AS standards.
"""
import statistics
from decimal import Decimal
from typing import Dict, List, Optional, Any


class FinancialFeatureExtractor:
    """
    Extracts ML features from a portfolio company's financial data.
    Uses MIS data (revenue, EBITDA, cash, debt, etc.) and BvA records.

    Outputs a flat dict of features suitable for XGBoost input.
    """

    def __init__(self, portfolio_company):
        self.company = portfolio_company

    def extract(self) -> Dict[str, Any]:
        """
        Main entry point. Returns a dict of features:
        - financial_ratios: D/E, Current Ratio, EBITDA margin, etc.
        - trend_features: QoQ % change in revenue, EBITDA, cash
        - z_score_features: deviation from sector mean
        - variance_features: BvA variance signals
        """
        features = {}
        features.update(self._financial_ratios())
        features.update(self._trend_features())
        features.update(self._z_score_features())
        features.update(self._bva_variance_features())
        features.update(self._kpi_features())
        return features

    def _financial_ratios(self) -> Dict[str, float]:
        """Compute key financial ratios from latest MIS data."""
        from investments.models import MISData
        from django.db.models import Max

        try:
            latest = MISData.objects.filter(
                portfolio_company=self.company
            ).order_by('-period_year', '-period_month').first()
        except Exception:
            latest = None

        if not latest:
            return self._null_ratios()

        ratios = {}

        # Liquidity
        if latest.current_assets and latest.current_liabilities and latest.current_liabilities != 0:
            ratios['current_ratio'] = float(latest.current_assets / latest.current_liabilities)
        else:
            ratios['current_ratio'] = None

        # Leverage
        if latest.total_debt and latest.net_worth and latest.net_worth != 0:
            ratios['debt_equity_ratio'] = float(latest.total_debt / latest.net_worth)
        else:
            ratios['debt_equity_ratio'] = None

        # Profitability
        if latest.ebitda is not None and latest.revenue and latest.revenue != 0:
            ratios['ebitda_margin'] = float(latest.ebitda / latest.revenue * 100)
        else:
            ratios['ebitda_margin'] = None

        if latest.pat is not None and latest.revenue and latest.revenue != 0:
            ratios['pat_margin'] = float(latest.pat / latest.revenue * 100)
        else:
            ratios['pat_margin'] = None

        # Cash burn
        if latest.cash_and_equivalents is not None and latest.monthly_burn and latest.monthly_burn != 0:
            ratios['cash_runway_months'] = float(latest.cash_and_equivalents / latest.monthly_burn)
        else:
            ratios['cash_runway_months'] = None

        # Interest coverage (EBITDA / Interest)
        if latest.ebitda and latest.finance_cost and latest.finance_cost != 0:
            ratios['interest_coverage'] = float(latest.ebitda / latest.finance_cost)
        else:
            ratios['interest_coverage'] = None

        # Asset turnover
        if latest.revenue and latest.total_assets and latest.total_assets != 0:
            ratios['asset_turnover'] = float(latest.revenue / latest.total_assets)
        else:
            ratios['asset_turnover'] = None

        return ratios

    def _trend_features(self) -> Dict[str, float]:
        """
        QoQ change (%) for key metrics over last 3 periods.
        Returns pct change for: revenue, ebitda, cash, total_debt.
        """
        from investments.models import MISData
        records = list(MISData.objects.filter(
            portfolio_company=self.company,
        ).order_by('-period_year', '-period_month')[:3])

        if len(records) < 2:
            return {
                'revenue_qoq_pct': None,
                'ebitda_qoq_pct': None,
                'cash_qoq_pct': None,
                'debt_qoq_pct': None,
            }

        curr, prev = records[0], records[1]

        def pct_change(new, old):
            if old is None or new is None or old == 0:
                return None
            return float((new - old) / abs(old) * 100)

        return {
            'revenue_qoq_pct': pct_change(curr.revenue, prev.revenue),
            'ebitda_qoq_pct': pct_change(curr.ebitda, prev.ebitda),
            'cash_qoq_pct': pct_change(curr.cash_and_equivalents, prev.cash_and_equivalents),
            'debt_qoq_pct': pct_change(curr.total_debt, prev.total_debt),
        }

    def _z_score_features(self) -> Dict[str, float]:
        """
        Z-score of this company's key metrics vs sector peers.
        Uses all companies in the same sector within the same organization.
        """
        from investments.models import MISData, PortfolioCompany
        sector = self.company.sector
        if not sector:
            return {}

        peers = PortfolioCompany.objects.filter(
            fund__organization=self.company.fund.organization,
            sector=sector,
        ).exclude(pk=self.company.pk)

        sector_ratios = {'ebitda_margin': [], 'debt_equity_ratio': [], 'current_ratio': []}
        for peer in peers:
            peer_features = FinancialFeatureExtractor(peer)._financial_ratios()
            for key in sector_ratios:
                if peer_features.get(key) is not None:
                    sector_ratios[key].append(peer_features[key])

        my_ratios = self._financial_ratios()
        z_scores = {}
        for key, peer_values in sector_ratios.items():
            if len(peer_values) < 2:
                z_scores[f'{key}_z_score'] = None
                continue
            mean = statistics.mean(peer_values)
            stdev = statistics.stdev(peer_values)
            if stdev == 0:
                z_scores[f'{key}_z_score'] = 0.0
                continue
            my_val = my_ratios.get(key)
            if my_val is None:
                z_scores[f'{key}_z_score'] = None
            else:
                z_scores[f'{key}_z_score'] = (my_val - mean) / stdev
        return z_scores

    def _bva_variance_features(self) -> Dict[str, float]:
        """
        Extract BvA variance signals from MIS Consolidation.
        Features: max_variance_pct, count_adverse_variances.
        """
        try:
            from mis_consolidation.models import BudgetVsActual
            bva_qs = BudgetVsActual.objects.filter(
                portfolio_company=self.company,
                variance_pct__isnull=False,
            ).order_by('-period_year', '-period_month')[:24]

            variances = [float(r.variance_pct) for r in bva_qs]
            if not variances:
                return {'max_adverse_variance_pct': None, 'adverse_variance_count': 0}

            adverse = [v for v in variances if v < -10]  # >10% unfavorable
            return {
                'max_adverse_variance_pct': min(variances) if variances else None,
                'adverse_variance_count': len(adverse),
            }
        except Exception:
            return {'max_adverse_variance_pct': None, 'adverse_variance_count': 0}

    def _kpi_features(self) -> Dict[str, float]:
        """Extract latest values for sector-specific KPIs."""
        from investments.models import PortfolioKPI
        kpis = PortfolioKPI.objects.filter(
            portfolio_company=self.company,
        ).order_by('-period_start').select_related('kpi_definition')[:20]

        features = {}
        for kpi in kpis:
            key = f'kpi_{kpi.kpi_definition.slug}'
            if kpi.value is not None:
                try:
                    features[key] = float(kpi.value)
                except (ValueError, TypeError):
                    pass
        return features

    def _null_ratios(self) -> Dict:
        return {
            'current_ratio': None, 'debt_equity_ratio': None,
            'ebitda_margin': None, 'pat_margin': None,
            'cash_runway_months': None, 'interest_coverage': None,
            'asset_turnover': None,
        }


class XGBoostRiskScorer:
    """
    Phase 2 ML risk scoring using XGBoost.
    Falls back to rule-based (Phase 1) if model is not yet trained.

    Training: Call XGBoostRiskScorer.train(training_data) to train and persist.
    Inference: XGBoostRiskScorer(company).predict() → (score, tier)
    """

    MODEL_PATH = None  # Set to file path when model is trained

    def __init__(self, portfolio_company):
        self.company = portfolio_company
        self.extractor = FinancialFeatureExtractor(portfolio_company)

    def predict(self) -> Dict[str, Any]:
        """
        Returns: {'risk_score': float, 'risk_tier': str, 'method': str, 'features': dict}
        """
        features = self.extractor.extract()

        # Try XGBoost first
        try:
            score, tier = self._xgboost_predict(features)
            return {'risk_score': score, 'risk_tier': tier, 'method': 'xgboost', 'features': features}
        except Exception:
            pass

        # Fallback: rule-based scoring
        score, tier = self._rule_based_score(features)
        return {'risk_score': score, 'risk_tier': tier, 'method': 'rule_based', 'features': features}

    def _xgboost_predict(self, features: Dict) -> tuple:
        """XGBoost model inference."""
        import xgboost as xgb
        import numpy as np
        import os

        if not self.MODEL_PATH or not os.path.exists(self.MODEL_PATH):
            raise FileNotFoundError('XGBoost model not trained yet')

        model = xgb.Booster()
        model.load_model(self.MODEL_PATH)

        feature_vector = self._features_to_vector(features)
        dmatrix = xgb.DMatrix(np.array([feature_vector]))
        score_raw = float(model.predict(dmatrix)[0])
        score = max(0.0, min(100.0, score_raw * 100))
        tier = self._score_to_tier(score)
        return score, tier

    def _rule_based_score(self, features: Dict) -> tuple:
        """
        Phase 1 rule-based scoring — weighted sum of 10 signals.
        Each signal scores 0-10; weights sum to 1.0.
        """
        signals = {}

        # Signal 1: Revenue vs Plan (BvA variance)
        var = features.get('max_adverse_variance_pct')
        if var is None:
            signals['revenue_vs_plan'] = 5.0  # neutral if no data
        elif var > -10:
            signals['revenue_vs_plan'] = 2.0  # within 10%
        elif var > -25:
            signals['revenue_vs_plan'] = 5.0
        elif var > -50:
            signals['revenue_vs_plan'] = 7.0
        else:
            signals['revenue_vs_plan'] = 10.0

        # Signal 2: EBITDA margin trend
        ebitda_margin = features.get('ebitda_margin')
        ebitda_qoq = features.get('ebitda_qoq_pct')
        if ebitda_margin is None:
            signals['ebitda_margin_trend'] = 5.0
        elif ebitda_margin > 20 and (ebitda_qoq or 0) > 0:
            signals['ebitda_margin_trend'] = 1.0
        elif ebitda_margin > 10:
            signals['ebitda_margin_trend'] = 4.0
        elif ebitda_margin > 0:
            signals['ebitda_margin_trend'] = 6.0
        else:
            signals['ebitda_margin_trend'] = 9.0

        # Signal 3: Cash burn / runway
        runway = features.get('cash_runway_months')
        if runway is None:
            signals['cash_burn_runway'] = 5.0
        elif runway > 18:
            signals['cash_burn_runway'] = 1.0
        elif runway > 12:
            signals['cash_burn_runway'] = 3.0
        elif runway > 6:
            signals['cash_burn_runway'] = 6.0
        else:
            signals['cash_burn_runway'] = 9.0

        # Signal 4: Working capital (current ratio)
        current_ratio = features.get('current_ratio')
        if current_ratio is None:
            signals['working_capital'] = 5.0
        elif current_ratio > 2.0:
            signals['working_capital'] = 1.0
        elif current_ratio > 1.5:
            signals['working_capital'] = 3.0
        elif current_ratio > 1.0:
            signals['working_capital'] = 6.0
        else:
            signals['working_capital'] = 9.0

        # Signal 5: Debt service (D/E ratio)
        de_ratio = features.get('debt_equity_ratio')
        if de_ratio is None:
            signals['debt_service'] = 4.0
        elif de_ratio < 0.5:
            signals['debt_service'] = 1.0
        elif de_ratio < 1.0:
            signals['debt_service'] = 3.0
        elif de_ratio < 2.0:
            signals['debt_service'] = 5.0
        elif de_ratio < 3.0:
            signals['debt_service'] = 7.0
        else:
            signals['debt_service'] = 9.0

        # Signals 6-10: Set to neutral (5.0) — require additional data sources
        signals['customer_concentration'] = 5.0
        signals['mgmt_changes'] = 5.0
        signals['market_conditions'] = 5.0
        signals['peer_comparisons'] = 5.0 if features.get('ebitda_margin_z_score') is None else max(
            0, min(10, 5.0 + features['ebitda_margin_z_score'])
        )
        signals['compliance_status'] = 5.0

        # Weights (sum to 1.0)
        weights = {
            'revenue_vs_plan': 0.15,
            'ebitda_margin_trend': 0.15,
            'cash_burn_runway': 0.15,
            'working_capital': 0.10,
            'debt_service': 0.10,
            'customer_concentration': 0.10,
            'mgmt_changes': 0.05,
            'market_conditions': 0.10,
            'peer_comparisons': 0.05,
            'compliance_status': 0.05,
        }

        composite = sum(signals[k] * weights[k] for k in signals) * 10  # 0-100 scale
        tier = self._score_to_tier(composite)
        return composite, tier

    @staticmethod
    def _score_to_tier(score: float) -> str:
        if score <= 33:
            return 'low'
        elif score <= 66:
            return 'medium'
        else:
            return 'high'

    def _features_to_vector(self, features: Dict) -> List[float]:
        """Convert feature dict to ordered numpy vector for XGBoost."""
        FEATURE_ORDER = [
            'current_ratio', 'debt_equity_ratio', 'ebitda_margin', 'pat_margin',
            'cash_runway_months', 'interest_coverage', 'asset_turnover',
            'revenue_qoq_pct', 'ebitda_qoq_pct', 'cash_qoq_pct', 'debt_qoq_pct',
            'ebitda_margin_z_score', 'debt_equity_ratio_z_score', 'current_ratio_z_score',
            'max_adverse_variance_pct', 'adverse_variance_count',
        ]
        return [float(features.get(k) or 0.0) for k in FEATURE_ORDER]

    @classmethod
    def train(cls, training_records: List[Dict], model_save_path: str):
        """
        Train XGBoost model on historical risk records.
        training_records: list of {'features': dict, 'actual_risk_score': float}
        """
        import xgboost as xgb
        import numpy as np

        X = []
        y = []
        dummy = cls(None)
        for record in training_records:
            vec = dummy._features_to_vector(record['features'])
            X.append(vec)
            y.append(float(record['actual_risk_score']) / 100.0)  # normalize to 0-1

        X = np.array(X)
        y = np.array(y)

        dtrain = xgb.DMatrix(X, label=y)
        params = {
            'max_depth': 4,
            'eta': 0.1,
            'objective': 'reg:squarederror',
            'eval_metric': 'rmse',
            'subsample': 0.8,
            'colsample_bytree': 0.8,
            'seed': 42,
        }
        model = xgb.train(params, dtrain, num_boost_round=100)
        model.save_model(model_save_path)
        cls.MODEL_PATH = model_save_path
        return model


class ExitSignalEngine:
    """
    AI-powered exit timing and route recommendation.
    Analyzes: current valuation, market conditions, fund life, MOIC/IRR trajectory.
    Returns: exit_recommendation (dict with timing, route, rationale from Gemini).
    """

    def __init__(self, portfolio_company, investment=None):
        self.company = portfolio_company
        self.investment = investment

    def analyze(self) -> Dict[str, Any]:
        """
        Returns exit signal analysis:
        {
          'exit_score': 0-100 (higher = exit now),
          'recommended_timing': 'immediate|1_year|2_year|hold',
          'recommended_route': 'ipo|strategic_sale|secondary|pe_buyout|management_buyout',
          'key_signals': [...],
          'ai_rationale': str,
          'current_moic': float,
          'projected_irr_at_exit': float,
        }
        """
        signals = self._compute_exit_signals()
        ai_rationale = self._get_ai_rationale(signals)
        return {**signals, 'ai_rationale': ai_rationale}

    def _compute_exit_signals(self) -> Dict[str, Any]:
        """Rule-based exit signal computation."""
        from investments.models import Investment, Valuation
        from django.db.models import Max
        import datetime

        result = {
            'exit_score': 50,
            'recommended_timing': 'hold',
            'recommended_route': 'strategic_sale',
            'key_signals': [],
            'current_moic': None,
            'projected_irr_at_exit': None,
        }

        if not self.investment:
            try:
                self.investment = self.company.investments.order_by('investment_date').first()
            except Exception:
                return result

        if not self.investment:
            return result

        score = 0
        signals = []

        # Signal 1: MOIC trajectory
        try:
            latest_val = self.investment.valuations.order_by('-valuation_date').first()
            if latest_val and self.investment.amount_invested:
                moic = float(latest_val.fair_value_of_holding / self.investment.amount_invested)
                result['current_moic'] = moic
                if moic >= 5.0:
                    score += 30
                    signals.append(f'MOIC {moic:.1f}× — strong exit multiple')
                elif moic >= 3.0:
                    score += 20
                    signals.append(f'MOIC {moic:.1f}× — attractive exit multiple')
                elif moic >= 2.0:
                    score += 10
                    signals.append(f'MOIC {moic:.1f}× — acceptable')
                else:
                    score -= 10
                    signals.append(f'MOIC {moic:.1f}× — below target, hold for growth')
        except Exception:
            pass

        # Signal 2: Fund life check
        try:
            fund = self.company.fund
            if fund.vintage_year:
                years_since_vintage = datetime.date.today().year - fund.vintage_year
                if years_since_vintage >= 8:
                    score += 25
                    signals.append(f'Fund age {years_since_vintage}y — approaching end of life')
                elif years_since_vintage >= 6:
                    score += 15
                    signals.append(f'Fund age {years_since_vintage}y — exit window approaching')
        except Exception:
            pass

        # Signal 3: Financial momentum (revenue trend)
        features = FinancialFeatureExtractor(self.company).extract()
        rev_qoq = features.get('revenue_qoq_pct')
        if rev_qoq is not None:
            if rev_qoq > 20:
                score += 15
                signals.append(f'Revenue growth {rev_qoq:.0f}% QoQ — strong growth momentum for exit premium')
            elif rev_qoq < -10:
                score -= 10
                signals.append(f'Revenue declining {abs(rev_qoq):.0f}% QoQ — wait for recovery before exit')

        ebitda_margin = features.get('ebitda_margin')
        if ebitda_margin and ebitda_margin > 20:
            score += 10
            signals.append(f'EBITDA margin {ebitda_margin:.0f}% — high quality earnings support premium valuation')

        result['exit_score'] = max(0, min(100, 50 + score))
        result['key_signals'] = signals

        # Timing recommendation
        if result['exit_score'] >= 70:
            result['recommended_timing'] = 'immediate'
        elif result['exit_score'] >= 55:
            result['recommended_timing'] = '1_year'
        elif result['exit_score'] >= 40:
            result['recommended_timing'] = '2_year'
        else:
            result['recommended_timing'] = 'hold'

        # Route recommendation (simplified — based on company profile)
        try:
            revenue = float(self.company.investments.first().valuations.first().fair_value or 0)
            if revenue > 500:  # large company — IPO viable
                result['recommended_route'] = 'ipo'
            elif revenue > 100:
                result['recommended_route'] = 'strategic_sale'
            else:
                result['recommended_route'] = 'secondary'
        except Exception:
            pass

        return result

    def _get_ai_rationale(self, signals: Dict) -> str:
        """Call Gemini (Vertex AI) to generate natural language exit rationale."""
        try:
            from api.gemini_service import generate_content

            prompt = f"""You are a senior investment professional with 25 years of PE/VC experience in Indian markets.

Analyze this portfolio company's exit situation:
Company: {self.company.company_name}
Sector: {self.company.sector}
Exit Score: {signals['exit_score']}/100
Recommended Timing: {signals['recommended_timing']}
Recommended Route: {signals['recommended_route']}
Current MOIC: {signals.get('current_moic', 'N/A')}
Key Signals: {', '.join(signals.get('key_signals', []))}

Provide a concise 3-4 sentence professional exit recommendation covering:
1. Exit timing rationale
2. Preferred exit route and why
3. Key value creation actions before exit (if timing is not immediate)

Write in a professional tone suitable for an IC memo."""

            response = generate_content(prompt)
            return response.text.strip()
        except Exception:
            return self._rule_based_rationale(signals)

    def _rule_based_rationale(self, signals: Dict) -> str:
        """Fallback rationale without Gemini."""
        timing = signals.get('recommended_timing', 'hold')
        route = signals.get('recommended_route', 'strategic_sale')
        score = signals.get('exit_score', 50)

        timing_text = {
            'immediate': 'conditions are currently favorable for an exit',
            '1_year': 'an exit within 12 months is recommended',
            '2_year': 'an exit within 18-24 months is the optimal window',
            'hold': 'holding for continued value creation is recommended',
        }.get(timing, 'a hold strategy is recommended')

        route_text = {
            'ipo': 'an IPO or strategic PE buyout',
            'strategic_sale': 'a strategic sale to an industry player',
            'secondary': 'a secondary sale to another PE/growth fund',
            'pe_buyout': 'a PE buyout / sponsor-to-sponsor transaction',
        }.get(route, 'a strategic sale')

        return (
            f'Based on an exit score of {score}/100, {timing_text}. '
            f'The preferred exit route is {route_text}. '
            f'Key signals: {"; ".join(signals.get("key_signals", ["Insufficient data"]))[:200]}.'
        )
