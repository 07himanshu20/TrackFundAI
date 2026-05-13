from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('investments', '0004_valuation_ipev_fields_kpidefinition_sector'),
    ]

    operations = [
        migrations.CreateModel(
            name='ListedSecurityMapping',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('exchange', models.CharField(
                    max_length=10,
                    choices=[
                        ('bse', 'BSE (Bombay Stock Exchange)'),
                        ('nse', 'NSE (National Stock Exchange)'),
                        ('nyse', 'NYSE'),
                        ('nasdaq', 'NASDAQ'),
                        ('lse', 'LSE (London)'),
                        ('sgx', 'SGX (Singapore)'),
                    ],
                )),
                ('ticker_symbol', models.CharField(max_length=30, help_text='e.g., RELIANCE, TCS, INFY.NS')),
                ('isin', models.CharField(max_length=12, blank=True, help_text='ISIN code (12 chars)')),
                ('is_primary_listing', models.BooleanField(default=True)),
                ('currency', models.CharField(max_length=3, default='INR')),
                ('shares_held', models.DecimalField(
                    max_digits=18, decimal_places=4, default=0,
                    help_text='Number of shares held by the fund — updated on corporate actions',
                )),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('portfolio_company', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='listed_securities',
                    to='investments.portfoliocompany',
                )),
            ],
            options={'ordering': ['portfolio_company', 'exchange']},
        ),
        migrations.AlterUniqueTogether(
            name='listedsecuritymapping',
            unique_together={('portfolio_company', 'exchange', 'ticker_symbol')},
        ),
        migrations.CreateModel(
            name='MarketPriceFeed',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('price_date', models.DateField()),
                ('close_price', models.DecimalField(max_digits=14, decimal_places=4)),
                ('open_price', models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)),
                ('high_price', models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)),
                ('low_price', models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)),
                ('volume', models.BigIntegerField(null=True, blank=True)),
                ('currency', models.CharField(max_length=3, default='INR')),
                ('source', models.CharField(
                    max_length=15, default='bse_api',
                    choices=[
                        ('bse_api', 'BSE India API'),
                        ('nse_api', 'NSE India API'),
                        ('bloomberg', 'Bloomberg'),
                        ('alpha_vantage', 'Alpha Vantage'),
                        ('manual', 'Manual Entry'),
                    ],
                )),
                ('fair_value_of_holding', models.DecimalField(
                    max_digits=18, decimal_places=2, null=True, blank=True,
                    help_text='close_price × security.shares_held — auto-computed on insert',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('security', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='price_feed',
                    to='marketdata.listedsecuritymapping',
                )),
            ],
            options={'ordering': ['security', '-price_date']},
        ),
        migrations.AlterUniqueTogether(
            name='marketpricefeed',
            unique_together={('security', 'price_date')},
        ),
        migrations.AddIndex(
            model_name='marketpricefeed',
            index=models.Index(fields=['security', 'price_date'], name='mktdata_security_date_idx'),
        ),
        migrations.CreateModel(
            name='FXRateFeed',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('base_currency', models.CharField(max_length=3, default='USD')),
                ('quote_currency', models.CharField(max_length=3, default='INR')),
                ('rate_date', models.DateField()),
                ('rate', models.DecimalField(max_digits=14, decimal_places=6)),
                ('source', models.CharField(max_length=50, default='rbi', help_text='rbi / bloomberg')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'ordering': ['-rate_date']},
        ),
        migrations.AlterUniqueTogether(
            name='fxratefeed',
            unique_together={('base_currency', 'quote_currency', 'rate_date')},
        ),
    ]
