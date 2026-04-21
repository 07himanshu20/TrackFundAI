"""
Management command to seed initial data for development:
  - Creates a default Organization (Trivesta Consulting)
  - Creates an admin user (admin / admin123)
  - Creates a sample Fund with one Scheme
  - Creates sample notifications

Usage:
  python manage.py seed_data
  python manage.py seed_data --reset   # wipe and re-seed
"""
from django.core.management.base import BaseCommand
from accounts.models import Organization, User
from funds.models import Fund, Scheme, Entity
from notifications.models import Notification


class Command(BaseCommand):
    help = 'Seed initial data for TrackFundAI development'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset', action='store_true',
            help='Delete existing seed data before re-creating',
        )

    def handle(self, *args, **options):
        if options['reset']:
            self.stdout.write('Resetting seed data...')
            User.objects.filter(username='admin').delete()
            Organization.objects.filter(slug='trivesta').delete()

        # 1. Organization
        org, created = Organization.objects.get_or_create(
            slug='trivesta',
            defaults={
                'name': 'Trivesta Consulting Pvt. Ltd.',
                'subscription_tier': 'enterprise',
            },
        )
        self._report('Organization', org.name, created)

        # 2. Admin user
        if not User.objects.filter(username='admin').exists():
            admin = User.objects.create_superuser(
                username='admin',
                email='admin@trivesta.in',
                password='admin123',
                first_name='Trivesta',
                last_name='Admin',
                organization=org,
                role='gp_admin',
            )
            self._report('User', admin.username, True)
        else:
            self.stdout.write(f'  User "admin" already exists — skipping')

        # 3. Sample GP user
        if not User.objects.filter(username='himanshu').exists():
            gp_user = User.objects.create_user(
                username='himanshu',
                email='himanshu@trivesta.in',
                password='himanshu123',
                first_name='Himanshu',
                last_name='Sharma',
                organization=org,
                role='gp_admin',
            )
            self._report('User', gp_user.username, True)
        else:
            self.stdout.write(f'  User "himanshu" already exists — skipping')

        # 4. Sample Fund
        fund, created = Fund.objects.get_or_create(
            organization=org,
            name='Trivesta Healthcare Fund I',
            defaults={
                'sebi_registration_number': 'IN/AIF2/25-26/XXXX',
                'category': 'cat_2',
                'structure_type': 'trust',
                'corpus_target': 500_000_000,
                'base_currency': 'INR',
                'description': 'Category II AIF focused on healthcare and life sciences',
            },
        )
        self._report('Fund', fund.name, created)

        # 5. Scheme under the fund
        scheme, created = Scheme.objects.get_or_create(
            fund=fund,
            name='Scheme I',
            defaults={
                'vintage_year': 2025,
                'scheme_size': 250_000_000,
                'hurdle_rate_pct': 8.00,
                'carry_pct': 20.00,
                'carry_type': 'european',
                'management_fee_basis': 'committed',
                'management_fee_pct': 2.00,
            },
        )
        self._report('Scheme', scheme.name, created)

        # 6. Key entities
        entities_data = [
            {
                'role': 'manager',
                'name': 'Trivesta Capital Advisors LLP',
                'contact_person': 'Himanshu Sharma',
                'email': 'himanshu@trivesta.in',
            },
            {
                'role': 'trustee',
                'name': 'Axis Trustee Services Ltd.',
                'contact_person': 'Trustee Office',
                'email': 'aif-trustee@axistrustee.in',
            },
            {
                'role': 'custodian',
                'name': 'Orbis Financial Corporation Ltd.',
                'contact_person': 'AIF Custody Desk',
                'email': 'custody@orbisfinancial.com',
            },
        ]
        for ent_data in entities_data:
            entity, created = Entity.objects.get_or_create(
                fund=fund,
                role=ent_data['role'],
                name=ent_data['name'],
                defaults=ent_data,
            )
            self._report('Entity', f'{entity.get_role_display()}: {entity.name}', created)

        # 7. Sample notifications
        admin_user = User.objects.filter(username='admin').first()
        himanshu_user = User.objects.filter(username='himanshu').first()
        if admin_user and not Notification.objects.filter(organization=org).exists():
            sample_notifs = [
                {
                    'recipient': admin_user,
                    'title': 'Fund Created',
                    'message': f'{fund.name} has been registered successfully.',
                    'category': 'fund',
                    'resource_type': 'fund',
                    'resource_id': str(fund.id),
                },
                {
                    'recipient': admin_user,
                    'title': 'Scheme Added',
                    'message': f'Scheme I has been added to {fund.name}.',
                    'category': 'fund',
                    'resource_type': 'scheme',
                    'resource_id': str(scheme.id),
                },
                {
                    'recipient': admin_user,
                    'title': 'Welcome to TrackFundAI',
                    'message': 'Your organization has been set up. Start by adding funds, schemes, and uploading documents.',
                    'category': 'system',
                    'priority': 'high',
                },
            ]
            if himanshu_user:
                sample_notifs.append({
                    'recipient': himanshu_user,
                    'title': 'Welcome to TrackFundAI',
                    'message': 'You have been added to Trivesta Consulting. Explore your funds and documents.',
                    'category': 'system',
                })
            for n_data in sample_notifs:
                Notification.objects.create(organization=org, **n_data)
            self.stdout.write(self.style.SUCCESS(f'  Created {len(sample_notifs)} sample notifications'))
        else:
            self.stdout.write('  Notifications already exist — skipping')

        self.stdout.write(self.style.SUCCESS('\nSeed data complete!'))
        self.stdout.write(f'  Login: admin / admin123')
        self.stdout.write(f'  Org:   {org.name}')
        self.stdout.write(f'  Fund:  {fund.name}')

    def _report(self, model, name, created):
        action = 'Created' if created else 'Exists'
        style = self.style.SUCCESS if created else self.style.WARNING
        self.stdout.write(style(f'  {action} {model}: {name}'))
