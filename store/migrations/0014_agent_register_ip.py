from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0013_mailgatewaysyncconfig"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="register_ip",
            field=models.CharField(blank=True, default="", max_length=64, verbose_name="注册IP"),
        ),
    ]
