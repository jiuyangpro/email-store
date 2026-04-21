from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from store.models import StockItem


class Command(BaseCommand):
    help = "清理已售且超过保留期的库存内容，保留订单与金额记录。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="已售库存保留天数，默认 7 天。满 7 天后，从第 8 天开始可清理。",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只统计将会删除多少条，不真正删除。",
        )

    def handle(self, *args, **options):
        days = max(0, options["days"])
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)
        queryset = StockItem.objects.filter(
            is_sold=True,
            sold_at__isnull=False,
            sold_at__lte=cutoff,
        )
        matched_count = queryset.count()

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"演练完成：将清理 {matched_count} 条已售库存，订单与金额记录不会删除。"
                )
            )
            return

        deleted_count, _ = queryset.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"清理完成：已删除 {deleted_count} 条超过 {days} 天的已售库存，订单与金额记录已保留。"
            )
        )
