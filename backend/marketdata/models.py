"""
Market Data Feed models — BSE / NSE / Bloomberg price ingestion.

Used for:
  - IPEV Level 1 valuation (CMP × shares for listed holdings)
  - Daily auto-update of fair_value_of_holding for listed securities
  - FX rate feeds (USD/INR, EUR/INR, etc.)
  - RBI interest rate feed (used in DCF hurdle rate benchmarking)
"""

import uuid
from django.db import models


class ListedSecurityMapping(models.Model):
    """
    Maps a PortfolioCompany's listed security to an exchange ticker/ISIN.
    One company may have multiple listings (BSE + NSE, India + US ADR, etc.).
    """
    EXCHANGE_CHOICES = [
        ('bse',       'BSE (Bombay Stock Exchange)'),
        ('nse',       'NSE (National Stock Exchange)'),
        ('nyse',      'NYSE'),
        ('nasdaq',    'NASDAQ'),
        ('lse',       'LSE (London)'),
        ('sgx',       'SGX (Singapore)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    portfolio_company = models.ForeignKey(
        'investments.PortfolioCompany',
        on_delete=models.CASCADE,
        related_name='listed_securities',
    )
    exchange = models.CharField(max_length=10, choices=EXCHANGE_CHOICES)
    ticker_symbol = models.CharField(max_length=30, help_text='e.g., RELIANCE, TCS, INFY.NS')
    isin = models.CharField(max_length=12, blank=True, help_text='ISIN code (12 chars)')
    is_primary_listing = models.BooleanField(default=True)
    currency = models.CharField(max_length=3, default='INR')

    # Share count held by the fund (used for daily FV calculation)
    shares_held = models.DecimalField(
        max_digits=18, decimal_places=4, default=0,
        help_text='Number of shares held by the fund — updated on corporate actions',
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['portfolio_company', 'exchange']
        unique_together = ('portfolio_company', 'exchange', 'ticker_symbol')

    def __str__(self):
        return f'{self.portfolio_company.name} — {self.ticker_symbol} ({self.exchange.upper()})'


class MarketPriceFeed(models.Model):
    """
    Daily closing price for a listed security.
    Populated by the market data fetcher task.
    Delta detection: only insert if price changed from previous day.
    """
    SOURCE_CHOICES = [
        ('bse_api',      'BSE India API'),
        ('nse_api',      'NSE India API'),
        ('bloomberg',    'Bloomberg'),
        ('alpha_vantage', 'Alpha Vantage'),
        ('manual',       'Manual Entry'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    security = models.ForeignKey(
        ListedSecurityMapping,
        on_delete=models.CASCADE,
        related_name='price_feed',
    )
    price_date = models.DateField()
    close_price = models.DecimalField(max_digits=14, decimal_places=4)
    open_price  = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    high_price  = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    low_price   = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    volume      = models.BigIntegerField(null=True, blank=True)
    currency    = models.CharField(max_length=3, default='INR')
    source      = models.CharField(max_length=15, choices=SOURCE_CHOICES, default='bse_api')

    # Computed fair value of fund's holding = close_price × shares_held
    fair_value_of_holding = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text='close_price × security.shares_held — auto-computed on insert',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['security', '-price_date']
        unique_together = ('security', 'price_date')
        indexes = [
            models.Index(fields=['security', 'price_date']),
        ]

    def save(self, *args, **kwargs):
        if self.security and self.close_price:
            shares = self.security.shares_held or 0
            self.fair_value_of_holding = self.close_price * shares
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.security.ticker_symbol} — {self.price_date} @ {self.close_price}'


class FXRateFeed(models.Model):
    """
    Daily FX rate from RBI / Bloomberg.
    Used for INR conversion of USD/EUR denominated investments.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    base_currency = models.CharField(max_length=3, default='USD')
    quote_currency = models.CharField(max_length=3, default='INR')
    rate_date = models.DateField()
    rate = models.DecimalField(max_digits=14, decimal_places=6)
    source = models.CharField(max_length=50, default='rbi', help_text='rbi / bloomberg')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-rate_date']
        unique_together = ('base_currency', 'quote_currency', 'rate_date')

    def __str__(self):
        return f'{self.base_currency}/{self.quote_currency} = {self.rate} ({self.rate_date})'
