"""
Management command to import portfolio.json into PortfolioSnapshot + PortfolioNode tables.
This is the one-time migration from JSON-file-based storage to PostgreSQL.

Usage:
  python manage.py import_portfolio_json
  python manage.py import_portfolio_json --json-path /path/to/portfolio.json
"""

import json
import os

from django.core.management.base import BaseCommand
from portfolio.models import PortfolioSnapshot, PortfolioNode


class Command(BaseCommand):
    help = 'Import portfolio.json into the database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--json-path',
            default=os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                '..', '..', '..', 'api', 'portfolio', 'portfolio.json',
            ),
            help='Path to portfolio.json file',
        )

    def handle(self, *args, **options):
        json_path = os.path.abspath(options['json_path'])
        if not os.path.exists(json_path):
            self.stderr.write(self.style.ERROR(f'File not found: {json_path}'))
            return

        with open(json_path, 'r') as f:
            doc = json.load(f)

        self.stdout.write(f'Loading from: {json_path}')

        # Deactivate existing snapshots
        PortfolioSnapshot.objects.filter(is_active=True).update(is_active=False)

        # Create a new snapshot
        snapshot = PortfolioSnapshot.objects.create(
            schema_version=doc.get('schema_version', '2.0'),
            base_currency=doc.get('base_currency', 'USD'),
            fx_as_of=doc.get('fx_as_of', ''),
            fx_rates=doc.get('fx_rates', {}),
            period_range=doc.get('period_range', {}),
            source='json_import',
            is_active=True,
        )
        self.stdout.write(self.style.SUCCESS(f'Created snapshot: {snapshot.id}'))

        # Import all nodes recursively
        node_count = 0
        # First pass: create all nodes (without parent FK)
        node_map = {}  # node_id -> PortfolioNode

        def _import_node(node_data, parent_node_id, sort_order):
            nonlocal node_count
            node_id = node_data.get('id', '')
            if not node_id:
                return

            db_node = PortfolioNode(
                snapshot=snapshot,
                node_id=node_id,
                name=node_data.get('name', ''),
                level=node_data.get('level', 'company'),
                parent_node_id=parent_node_id,
                currency=node_data.get('currency', 'USD'),
                native_currency=node_data.get('native_currency'),
                is_real=node_data.get('is_real', False),
                description=node_data.get('description'),
                financials=node_data.get('financials', {}),
                sort_order=sort_order,
            )
            db_node.save()
            node_map[node_id] = db_node
            node_count += 1

            # Recurse into children
            for idx, child in enumerate(node_data.get('children', []) or []):
                _import_node(child, node_id, idx)

        for idx, fund in enumerate(doc.get('funds', [])):
            _import_node(fund, None, idx)

        # Second pass: set parent FK references
        for node_id, db_node in node_map.items():
            if db_node.parent_node_id:
                parent = node_map.get(db_node.parent_node_id)
                if parent:
                    db_node.parent = parent
                    db_node.save(update_fields=['parent'])

        self.stdout.write(self.style.SUCCESS(
            f'Imported {node_count} nodes into snapshot {snapshot.id}'
        ))
        self.stdout.write(f'  Funds: {PortfolioNode.objects.filter(snapshot=snapshot, level="fund").count()}')
        self.stdout.write(f'  Sectors: {PortfolioNode.objects.filter(snapshot=snapshot, level="sector").count()}')
        self.stdout.write(f'  Segments: {PortfolioNode.objects.filter(snapshot=snapshot, level="segment").count()}')
        self.stdout.write(f'  Companies: {PortfolioNode.objects.filter(snapshot=snapshot, level="company").count()}')
