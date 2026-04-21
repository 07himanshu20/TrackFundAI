"""
Management command to rebuild the portfolio from all company Excel files
and save to both PostgreSQL and portfolio.json.

This replaces the old `python -m api.portfolio.builder` workflow.

Usage:
  python manage.py build_portfolio              # rebuild from Excel, save to DB + JSON
  python manage.py build_portfolio --db-only     # save to DB only (skip JSON)
  python manage.py build_portfolio --json-only   # save to JSON only (skip DB)
"""

import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Rebuild portfolio from company Excel files and save to DB + JSON'

    def add_arguments(self, parser):
        parser.add_argument(
            '--db-only', action='store_true',
            help='Save to database only, skip JSON file',
        )
        parser.add_argument(
            '--json-only', action='store_true',
            help='Save to JSON file only, skip database',
        )

    def handle(self, *args, **options):
        from api.portfolio.builder import build_portfolio, save_portfolio, save_portfolio_to_db
        from api.portfolio.service import PORTFOLIO_JSON_PATH

        self.stdout.write('Building portfolio from company Excel files...')

        try:
            doc = build_portfolio()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'Build failed: {e}'))
            raise

        fund_count = len(doc.get('funds', []))
        period_range = doc.get('period_range', {})
        self.stdout.write(self.style.SUCCESS(
            f'Built portfolio: {fund_count} funds, '
            f'period {period_range.get("start", "?")} → {period_range.get("end", "?")}'
        ))

        # Count nodes
        def count_nodes(node):
            total = 1
            for child in node.get('children', []) or []:
                total += count_nodes(child)
            return total

        total_nodes = sum(count_nodes(f) for f in doc.get('funds', []))
        self.stdout.write(f'  Total nodes: {total_nodes}')

        if not options['json_only']:
            self.stdout.write('Saving to database...')
            save_portfolio_to_db(doc)
            self.stdout.write(self.style.SUCCESS('  Saved to PostgreSQL'))

        if not options['db_only']:
            self.stdout.write(f'Saving to {PORTFOLIO_JSON_PATH}...')
            save_portfolio(PORTFOLIO_JSON_PATH)
            self.stdout.write(self.style.SUCCESS('  Saved to JSON'))

        self.stdout.write(self.style.SUCCESS('\nPortfolio rebuild complete!'))
