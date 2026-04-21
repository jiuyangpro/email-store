from django.db import migrations, models


def create_default_site_contact_config(apps, schema_editor):
    SiteContactConfig = apps.get_model("store", "SiteContactConfig")
    if not SiteContactConfig.objects.exists():
        SiteContactConfig.objects.create(title="官网联系方式")


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0011_stockitem_inbox_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="contact_image_1",
            field=models.ImageField(blank=True, null=True, upload_to="contact_images/agents/", verbose_name="联系方式图片1"),
        ),
        migrations.AddField(
            model_name="agent",
            name="contact_image_2",
            field=models.ImageField(blank=True, null=True, upload_to="contact_images/agents/", verbose_name="联系方式图片2"),
        ),
        migrations.CreateModel(
            name="SiteContactConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(default="官网联系方式", max_length=80, verbose_name="配置名称")),
                ("contact_image_1", models.ImageField(blank=True, null=True, upload_to="contact_images/site/", verbose_name="官网联系方式图片1")),
                ("contact_image_2", models.ImageField(blank=True, null=True, upload_to="contact_images/site/", verbose_name="官网联系方式图片2")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "官网联系方式配置",
                "verbose_name_plural": "官网联系方式配置",
                "ordering": ["id"],
            },
        ),
        migrations.RunPython(create_default_site_contact_config, migrations.RunPython.noop),
    ]
