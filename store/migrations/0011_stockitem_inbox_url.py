from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0008_agent_contact_phone_agent_contact_qq_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockitem",
            name="inbox_url",
            field=models.CharField(blank=True, default="", max_length=255, verbose_name="接码网址"),
        ),
    ]
