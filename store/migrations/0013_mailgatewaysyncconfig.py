from django.db import migrations, models


def create_default_mail_gateway_sync_config(apps, schema_editor):
    MailGatewaySyncConfig = apps.get_model("store", "MailGatewaySyncConfig")
    if not MailGatewaySyncConfig.objects.exists():
        MailGatewaySyncConfig.objects.create(
            title="邮局白名单自动同步",
            auto_sync_on_import=False,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0012_sitecontactconfig_agent_contact_images"),
    ]

    operations = [
        migrations.CreateModel(
            name="MailGatewaySyncConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(default="邮局白名单自动同步", max_length=80, verbose_name="配置名称")),
                ("auto_sync_on_import", models.BooleanField(default=False, verbose_name="上传库存后自动同步白名单")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "邮局同步开关",
                "verbose_name_plural": "邮局同步开关",
                "ordering": ["id"],
            },
        ),
        migrations.RunPython(create_default_mail_gateway_sync_config, migrations.RunPython.noop),
    ]
