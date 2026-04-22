from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from store.models import StockItem


class Command(BaseCommand):
    help = "更新 2FA 状态，将超过一个月的已开通 2FA 账号更新为可登录油管状态"

    def handle(self, *args, **options):
        # 计算一个月前的时间
        one_month_ago = timezone.now() - timedelta(days=30)
        
        # 查找所有已开通 2FA 但不是可登录油管状态，且创建时间超过一个月的库存项
        stock_items = StockItem.objects.filter(
            twofa_status=StockItem.TWOFA_HAS,
            is_sold=False,
            created_at__lte=one_month_ago
        )
        
        updated_count = 0
        for item in stock_items:
            # 更新状态为已开通 2FA 可登录油管
            item.twofa_status = StockItem.TWOFA_HAS_YOUTUBE
            item.save(update_fields=["twofa_status"])
            updated_count += 1
        
        self.stdout.write(
            self.style.SUCCESS(
                f"更新完成：已将 {updated_count} 个超过一个月的已开通 2FA 账号更新为可登录油管状态"
            )
        )
