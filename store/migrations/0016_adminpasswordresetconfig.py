from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0015_agent_email_agent_email_verified_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdminPasswordResetConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(default="后台找回密码配置", max_length=80, verbose_name="配置名称")),
                ("reset_emails", models.TextField(blank=True, default="", verbose_name="管理员接收邮箱")),
                ("code_expire_minutes", models.PositiveIntegerField(default=10, verbose_name="验证码有效期(分钟)")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "后台找回密码配置",
                "verbose_name_plural": "后台找回密码配置",
                "ordering": ["id"],
            },
        ),
    ]
