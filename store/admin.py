import re
import json
from urllib.parse import urlencode

from django import forms
from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.http import HttpResponseRedirect
from django.db import transaction
from django.db import models as django_models
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.text import Truncator
from django.utils import timezone
from django.conf import settings
from types import MethodType

from .mail_gateway_sync import extract_emails_from_text, sync_emails_to_mail_gateway
from .models import (
    Agent,
    AdminPasswordResetConfig,
    AgentPackagePrice,
    Document,
    MailGatewaySyncConfig,
    Order,
    Package,
    SiteContactConfig,
    StockItem,
    build_contact_image_sha256,
    get_saved_image_sha256,
)

DEMO_PACKAGE_NAMES = {
    "引流起步包",
    "全套变现包",
    "单条卡密示例",
    "一组卡密示例",
}
DEFAULT_GROUP_INBOX_URL = "https://shop.lncbeidfr.asia/"


def _is_hidden_legacy_package_q():
    return django_models.Q(name__in=DEMO_PACKAGE_NAMES) | django_models.Q(name__startswith="旧演示_") | django_models.Q(
        name__endswith="旧重复商品"
    )


admin.site.site_header = "运营后台"
admin.site.site_title = "运营后台"
admin.site.index_title = "常用管理"
admin.site.enable_nav_sidebar = False

_original_admin_each_context = admin.site.each_context


def _custom_admin_each_context(self, request):
    context = _original_admin_each_context(request)
    today = timezone.localdate()
    paid_statuses = [Order.STATUS_PAID, Order.STATUS_ISSUE]
    paid_today_orders = Order.objects.filter(
        status__in=paid_statuses,
        paid_at__date=today,
    )
    summary = paid_today_orders.aggregate(
        today_sales_amount=Sum("amount"),
        today_paid_order_count=Count("id"),
        today_total_quantity=Sum("quantity"),
    )
    context["sales_summary"] = {
        "today": today,
        "today_sales_amount": summary["today_sales_amount"] or 0,
        "today_paid_order_count": summary["today_paid_order_count"] or 0,
        "today_total_quantity": summary["today_total_quantity"] or 0,
    }
    pending_agent_count = Agent.objects.filter(status=Agent.STATUS_PENDING).count()
    context["agent_review_summary"] = {
        "pending_count": pending_agent_count,
        "pending_url": f'{reverse("admin:store_agent_changelist")}?status__exact={Agent.STATUS_PENDING}',
        "all_url": reverse("admin:store_agent_changelist"),
    }
    package_changelist_url = reverse("admin:store_package_changelist")
    visible_packages = Package.objects.filter(delivery_mode=Package.DELIVERY_STOCK).exclude(_is_hidden_legacy_package_q())
    context["package_entry_summary"] = {
        "line_count": visible_packages.filter(stock_mode=Package.STOCK_LINE).count(),
        "group_count": visible_packages.filter(stock_mode=Package.STOCK_GROUP).count(),
        "line_url": f"{package_changelist_url}?delivery_mode__exact={Package.DELIVERY_STOCK}&stock_mode__exact={Package.STOCK_LINE}",
        "group_url": f"{package_changelist_url}?delivery_mode__exact={Package.DELIVERY_STOCK}&stock_mode__exact={Package.STOCK_GROUP}",
        "all_url": f"{package_changelist_url}?delivery_mode__exact={Package.DELIVERY_STOCK}",
    }
    context["package_guard_notice"] = "正式上架商品只保留 2 个：按条 1 个，按组 1 个。库存统一往这两个商品里导入。"
    return context


admin.site.each_context = MethodType(_custom_admin_each_context, admin.site)


class PackageAdminForm(forms.ModelForm):
    bulk_import_text = forms.CharField(
        label="批量导入按条库存",
        required=False,
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text="一行一条，保存后会自动导入到库存。",
    )
    bulk_import_groups = forms.CharField(
        label="批量导入按组库存",
        required=False,
        widget=forms.Textarea(attrs={"rows": 12}),
        help_text="推荐用 ====GROUP==== 或空一行作为分隔符，一段就是一组。",
    )
    bulk_import_text_file = forms.FileField(
        label="按条 TXT 文件上传",
        required=False,
        help_text="上传 .txt 文件，一行一条，适合大批量导入。",
    )
    bulk_import_groups_file = forms.FileField(
        label="按组 TXT 文件上传",
        required=False,
        help_text="上传 .txt 文件，推荐用 ====GROUP==== 或空一行作为分隔符。",
    )
    bulk_import_groups_url = forms.CharField(
        label="接码网址",
        required=False,
        initial=DEFAULT_GROUP_INBOX_URL,
        help_text="按组库存统一只走这一个口子，发货时统一带这个接码网址。",
    )
    allow_duplicate_import = forms.BooleanField(
        label="检测到重复库存也继续导入",
        required=False,
        help_text="如果检测到这批邮箱/整组内容已存在，勾选后仍继续导入。",
    )

    class Meta:
        model = Package
        fields = [
            "name",
            "subtitle",
            "description",
            "price",
            "agent_floor_price",
            "original_price",
            "stock_mode",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.import_target_package = None
        self.fields["name"].label = "商品名称"
        self.fields["subtitle"].label = "简短说明"
        self.fields["description"].label = "商品介绍"
        self.fields["price"].label = "售价"
        self.fields["original_price"].label = "划线价"
        self.fields["agent_floor_price"].label = "代理底价"
        self.fields["stock_mode"].label = "售卖类型"
        self.fields["is_active"].label = "是否上架"
        self.fields["name"].widget.attrs.update({"placeholder": "比如：按条购买 / 按组购买"})
        self.fields["subtitle"].widget.attrs.update({"placeholder": "比如：自动顺序发货 / 整组原样发货"})
        self.fields["description"].widget.attrs.update({"rows": 5, "placeholder": "这里写给买家看的说明"})
        self.fields["price"].help_text = "这里直接填卖价，比如 0.40 或 30.00"
        self.fields["original_price"].help_text = "不想显示原价可以填 0"
        self.fields["agent_floor_price"].help_text = "代理不能低于这个价格。比如按条 0.30，按组 25.00。"
        self.fields["agent_floor_price"].required = False
        if self.instance.pk and self.instance.agent_floor_price:
            self.fields["agent_floor_price"].initial = self.instance.agent_floor_price
        elif self.instance.pk and self.instance.price:
            self.fields["agent_floor_price"].initial = self.instance.price
        self.fields["stock_mode"].help_text = "按条购买选按条售卖；按组购买选按组售卖"
        self.fields["is_active"].help_text = "正式上架商品请始终只保留 2 个：按条 1 个，按组 1 个。"
        self.pending_line_contents = []
        self.pending_group_contents = []
        self.pending_group_payloads = []

    def _collect_import_payloads(self, stock_mode, cleaned_data):
        bulk_text = (cleaned_data.get("bulk_import_text") or "").strip()
        bulk_groups = (cleaned_data.get("bulk_import_groups") or "").strip()
        bulk_text_file = cleaned_data.get("bulk_import_text_file")
        bulk_groups_file = cleaned_data.get("bulk_import_groups_file")
        if bulk_text_file:
            bulk_text = _decode_upload_file(bulk_text_file)
        if bulk_groups_file:
            bulk_groups = _decode_upload_file(bulk_groups_file)

        pending_line_contents, pending_group_contents = _build_pending_imports(
            stock_mode=stock_mode,
            bulk_text=bulk_text,
            bulk_groups=bulk_groups,
        )
        pending_group_payloads = []
        if stock_mode == Package.STOCK_GROUP:
            primary_url = (cleaned_data.get("bulk_import_groups_url") or "").strip() or DEFAULT_GROUP_INBOX_URL
            pending_group_payloads.extend(
                {"content": group, "inbox_url": primary_url} for group in pending_group_contents
            )
        return pending_line_contents, pending_group_contents, pending_group_payloads

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("agent_floor_price"):
            cleaned_data["agent_floor_price"] = cleaned_data.get("price") or self.instance.price or 0
        stock_mode = cleaned_data.get("stock_mode")
        is_active = cleaned_data.get("is_active")
        if not stock_mode or not is_active:
            return cleaned_data

        bulk_text = (cleaned_data.get("bulk_import_text") or "").strip()
        bulk_groups = (cleaned_data.get("bulk_import_groups") or "").strip()
        bulk_text_file = cleaned_data.get("bulk_import_text_file")
        bulk_groups_file = cleaned_data.get("bulk_import_groups_file")
        has_import_payload = bool(
            bulk_text or bulk_groups or bulk_text_file or bulk_groups_file
        )

        duplicate_package = (
            Package.objects.filter(
                delivery_mode=Package.DELIVERY_STOCK,
                stock_mode=stock_mode,
                is_active=True,
            )
            .exclude(pk=self.instance.pk)
            .order_by("id")
            .first()
        )
        target_package = self.instance
        if duplicate_package:
            if not self.instance.pk and has_import_payload:
                # 新建页如果只是为了继续补货，就把本次导入转到现有正式商品，
                # 仍然保持“同类型只保留 1 个上架商品”的规则。
                self.import_target_package = duplicate_package
                target_package = duplicate_package
            else:
                mode_label = "按条售卖" if stock_mode == Package.STOCK_LINE else "按组售卖"
                raise forms.ValidationError(
                    f"{mode_label} 只能保留 1 个上架商品。请继续使用「{duplicate_package.name}」进行导入；"
                    "如需切换，请先把旧商品下架后再保存当前商品。"
                )

        (
            self.pending_line_contents,
            self.pending_group_contents,
            self.pending_group_payloads,
        ) = self._collect_import_payloads(stock_mode, cleaned_data)

        duplicate_values = []
        inner_duplicate_values = []
        if stock_mode == Package.STOCK_LINE and self.pending_line_contents:
            existing_contents = (
                set(StockItem.objects.filter(package=target_package).values_list("content", flat=True))
                if target_package.pk
                else set()
            )
            seen = set()
            for content in self.pending_line_contents:
                if content in existing_contents and content not in duplicate_values:
                    duplicate_values.append(content)
                if content in seen and content not in inner_duplicate_values:
                    inner_duplicate_values.append(content)
                seen.add(content)
        if stock_mode == Package.STOCK_GROUP and self.pending_group_payloads:
            existing_contents = (
                set(StockItem.objects.filter(package=target_package).values_list("content", flat=True))
                if target_package.pk
                else set()
            )
            seen = set()
            for payload in self.pending_group_payloads:
                content = payload["content"]
                if content in existing_contents and content not in duplicate_values:
                    duplicate_values.append(content)
                if content in seen and content not in inner_duplicate_values:
                    inner_duplicate_values.append(content)
                seen.add(content)

        if (duplicate_values or inner_duplicate_values) and not cleaned_data.get("allow_duplicate_import"):
            summary_parts = []
            if duplicate_values:
                summary_parts.append(f"发现 {len(duplicate_values)} 条内容和库存里已存在")
            if inner_duplicate_values:
                summary_parts.append(f"发现 {len(inner_duplicate_values)} 条内容在本次导入里重复")
            preview = duplicate_values[:2] or inner_duplicate_values[:2]
            preview_text = "；示例：" + " / ".join(Truncator(item).chars(60) for item in preview) if preview else ""
            self.add_error(
                "allow_duplicate_import",
                "，".join(summary_parts) + f"{preview_text}。如确认继续，请勾选“检测到重复库存也继续导入”。",
            )
        return cleaned_data

    def save(self, commit=True):
        instance = self.import_target_package or super().save(commit=commit)
        if self.import_target_package is not None and not commit:
            # Django admin add_view 在 save_form(commit=False) 后仍会调用 form.save_m2m()。
            # 当我们把“新增页补货”重定向到现有正式商品时，需要保留这个接口。
            self.save_m2m = self._save_m2m
        instance.delivery_mode = Package.DELIVERY_STOCK
        instance.items_per_unit = 1
        self.imported_line_count = 0
        self.imported_group_count = len(self.pending_group_payloads) if instance.stock_mode == Package.STOCK_GROUP else 0
        self._stock_import_done = False
        self.mail_gateway_sync_result = None
        if commit:
            if self.import_target_package is None:
                instance.save(update_fields=["delivery_mode", "items_per_unit"])
            self.import_stock_items(instance)
        return instance

    def import_stock_items(self, instance):
        if self._stock_import_done:
            return
        if instance.stock_mode == Package.STOCK_LINE and self.pending_line_contents:
            items = [StockItem(package=instance, content=content) for content in self.pending_line_contents]
            StockItem.objects.bulk_create(items)
            self.imported_line_count = len(items)
        if instance.stock_mode == Package.STOCK_GROUP and self.pending_group_payloads:
            items = [
                StockItem(package=instance, content=payload["content"], inbox_url=payload["inbox_url"])
                for payload in self.pending_group_payloads
            ]
            StockItem.objects.bulk_create(items)
            self.imported_group_count = len(items)
        imported_emails = []
        for content in self.pending_line_contents:
            imported_emails.extend(extract_emails_from_text(content))
        for payload in self.pending_group_payloads:
            imported_emails.extend(extract_emails_from_text(payload["content"]))
        if imported_emails:
            self.mail_gateway_sync_result = sync_emails_to_mail_gateway(
                imported_emails,
                notes=f"商城后台导入自动同步，商品 {instance.name}",
            )
        self._stock_import_done = True


def _split_group_blocks(text):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # 支持三种分组方式：
    # 1. ====GROUP==== 分隔符
    # 2. 空行分隔
    # 3. ------域名_数字-------- 格式的分组标记
    if "====GROUP====" in normalized:
        parts = normalized.split("====GROUP====")
    else:
        # 先按 ------域名_数字-------- 格式分割
        group_markers = re.findall(r'------[^_]+_\d+--------', normalized)
        if group_markers:
            parts = []
            current = normalized
            for marker in group_markers:
                if marker in current:
                    before, after = current.split(marker, 1)
                    if before.strip():
                        parts.append(before)
                    # 提取标记后的内容直到下一个标记
                    next_marker_idx = None
                    for next_marker in group_markers:
                        if next_marker != marker and next_marker in after:
                            if next_marker_idx is None or after.index(next_marker) < next_marker_idx:
                                next_marker_idx = after.index(next_marker)
                    if next_marker_idx is not None:
                        parts.append(marker + after[:next_marker_idx])
                        current = after[next_marker_idx:]
                    else:
                        parts.append(marker + after)
                        current = ""
            if current.strip():
                parts.append(current)
        else:
            # 空行分隔
            parts = re.split(r"\n\s*\n+", normalized)
    
    # 处理每个组，确保每组最多50个账号
    processed_parts = []
    for part in parts:
        if not part.strip():
            continue
        # 提取账号行
        lines = [line.strip() for line in part.splitlines() if line.strip() and "----" in line]
        # 每组最多50个账号
        if len(lines) > 50:
            # 超过50个账号时，分成多个组
            for i in range(0, len(lines), 50):
                group_lines = lines[i:i+50]
                # 保留分组标记（如果有）
                if "------" in part and i == 0:
                    # 找到分组标记
                    marker_match = re.search(r'------[^_]+_\d+--------', part)
                    if marker_match:
                        marker = marker_match.group(0)
                        processed_parts.append(marker + "\n" + "\n".join(group_lines))
                    else:
                        processed_parts.append("\n".join(group_lines))
                else:
                    processed_parts.append("\n".join(group_lines))
        else:
            processed_parts.append(part.strip())
    
    return processed_parts


def _split_line_blocks(text):
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            # 处理账号----密码----密钥 或 账号----密码 格式
            lines.append(stripped)
    return lines


def _build_pending_imports(stock_mode, bulk_text, bulk_groups):
    pending_line_contents = _split_line_blocks(bulk_text) if bulk_text else []
    pending_group_contents = _split_group_blocks(bulk_groups) if bulk_groups else []

    # For single-group补货，很多人会把整组内容直接贴到“按条”导入框里。
    # 按组商品这里做一次兼容：如果真正的按组输入为空，就把整段文本当成 1 组。
    if stock_mode == Package.STOCK_GROUP and not pending_group_contents and bulk_text.strip():
        pending_group_contents = [bulk_text.strip()]
        pending_line_contents = []

    # 反向兼容：按条商品如果误用了按组输入框，就自动打平成逐条库存。
    if stock_mode == Package.STOCK_LINE and not pending_line_contents and pending_group_contents:
        pending_line_contents = [
            line.strip()
            for group in pending_group_contents
            for line in group.splitlines()
            if line.strip()
        ]
        pending_group_contents = []

    return pending_line_contents, pending_group_contents


def _decode_upload_file(uploaded_file):
    raw = uploaded_file.read()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin1")


class StockItemInline(admin.TabularInline):
    model = StockItem
    extra = 1
    ordering = ("-is_sold", "-sold_at", "-id")
    fields = ("content", "inbox_url", "is_sold", "sold_order", "sold_at")
    readonly_fields = ("is_sold", "sold_order", "sold_at")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("title", "summary")


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    form = PackageAdminForm
    list_display = (
        "name",
        "stock_mode",
        "stock_totals",
        "stock_view_link",
        "price",
        "agent_floor_price",
        "original_price",
        "is_active",
        "created_at",
    )
    list_display_links = ("name",)
    list_editable = ("price", "original_price", "is_active")
    list_filter = ("delivery_mode", "stock_mode", "is_active", "created_at")
    search_fields = ("name", "subtitle", "description")
    inlines = ()
    actions = ("clear_sold_stock", "merge_unsold_stock_into_primary")
    fields = (
        "name",
        "subtitle",
        "description",
        "price",
        "agent_floor_price",
        "original_price",
        "stock_mode",
        "is_active",
        "bulk_import_text",
        "bulk_import_text_file",
        "bulk_import_groups",
        "bulk_import_groups_file",
        "bulk_import_groups_url",
        "allow_duplicate_import",
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request).annotate(
            unsold_stock_count=Count("stock_items", filter=Q(stock_items__is_sold=False))
        )
        return queryset.exclude(_is_hidden_legacy_package_q()).exclude(
            delivery_mode=Package.DELIVERY_STOCK,
            is_active=False,
            unsold_stock_count=0,
        )

    @admin.display(description="库存统计")
    def stock_totals(self, obj):
        sold_count = obj.stock_items.filter(is_sold=True).count()
        unsold_count = obj.stock_items.filter(is_sold=False).count()
        return f"未售 {unsold_count} / 已售 {sold_count}"

    @admin.display(description="库存列表")
    def stock_view_link(self, obj):
        changelist_url = reverse("admin:store_stockitem_changelist")
        return format_html(
            '<a href="{}?package__id__exact={}&is_sold__exact=0">查看未售库存</a> | '
            '<a href="{}?package__id__exact={}&is_sold__exact=1">查看已售库存</a>',
            changelist_url,
            obj.pk,
            changelist_url,
            obj.pk,
        )

    @admin.action(description="只清空已售库存")
    def clear_sold_stock(self, request, queryset):
        sold_queryset = StockItem.objects.filter(package__in=queryset, is_sold=True)
        deleted_count, _ = sold_queryset.delete()
        self.message_user(request, f"已清空 {deleted_count} 条已售库存，未售库存已保留。")

    @admin.action(description="合并选中商品的未售库存到一个正式商品，并下架其余重复商品")
    def merge_unsold_stock_into_primary(self, request, queryset):
        selected = list(queryset.order_by("id"))
        if len(selected) < 2:
            self.message_user(request, "请至少勾选 2 个同类型商品再执行合并。", level="warning")
            return

        stock_modes = {package.stock_mode for package in selected}
        if len(stock_modes) != 1:
            self.message_user(request, "只能合并同一种售卖类型；请分开勾选按条或按组商品。", level="warning")
            return

        primary = selected[0]
        duplicates = selected[1:]

        moved_count = 0
        deactivated_count = 0
        with transaction.atomic():
            for package in duplicates:
                moved_count += (
                    StockItem.objects.filter(package=package, is_sold=False).update(package=primary)
                )
                if package.is_active:
                    package.is_active = False
                    package.save(update_fields=["is_active"])
                    deactivated_count += 1

        self.message_user(
            request,
            f"已将 {len(duplicates)} 个重复商品的 {moved_count} 条未售库存并入「{primary.name}」，"
            f"并下架 {deactivated_count} 个重复商品。已售库存和历史订单保持原样。",
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if hasattr(form, "import_stock_items"):
            form.import_stock_items(obj)
            imported_line_count = getattr(form, "imported_line_count", 0)
            imported_group_count = getattr(form, "imported_group_count", 0)
            import_target_package = getattr(form, "import_target_package", None)
            if import_target_package is not None and not change:
                self.message_user(
                    request,
                    f"检测到已有正式商品，已自动把本次补货并入「{import_target_package.name}」。",
                )
            if imported_line_count:
                self.message_user(request, f"本次成功导入 {imported_line_count} 条按条库存。")
            if imported_group_count:
                self.message_user(request, f"本次成功导入 {imported_group_count} 组按组库存。")
            sync_result = getattr(form, "mail_gateway_sync_result", None)
            if sync_result:
                if sync_result.get("disabled"):
                    self.message_user(
                        request,
                        f"当前已关闭“上传库存后自动同步白名单”，本次识别到 {sync_result.get('count', 0)} 个邮箱，但未推送到邮局。",
                    )
                elif sync_result.get("ok"):
                    self.message_user(
                        request,
                        f"邮局白名单已自动同步：识别 {sync_result.get('count', 0)} 个邮箱，"
                        f"新增 {sync_result.get('inserted', 0)}，更新 {sync_result.get('updated', 0)}，跳过 {sync_result.get('skipped', 0)}。",
                    )
                else:
                    self.message_user(
                        request,
                        f"库存已导入，但邮局白名单自动同步失败：{sync_result.get('detail') or sync_result.get('error')}",
                        level='warning',
                    )


class AgentPackagePriceInline(admin.TabularInline):
    model = AgentPackagePrice
    extra = 0
    fields = ("package", "sale_price", "is_enabled", "updated_at")
    readonly_fields = ("updated_at",)


def _contact_image_hashes(obj, field_names):
    hashes = []
    for field_name in field_names:
        file_field = getattr(obj, field_name, None)
        image_hash = get_saved_image_sha256(file_field)
        if image_hash:
            hashes.append(image_hash)
    return hashes


class DuplicateGuardMixin:
    duplicate_guard_fields = ()

    def _apply_duplicate_guard_attrs(self, instance):
        existing_hashes = json.dumps(_contact_image_hashes(instance, self.duplicate_guard_fields))
        for field_name in self.duplicate_guard_fields:
            if field_name in self.fields:
                self.fields[field_name].widget.attrs.update(
                    {
                        "data-duplicate-guard": "true",
                        "data-existing-hashes": existing_hashes,
                        "data-allow-field": f"allow_duplicate_{field_name}",
                    }
                )

    def _validate_duplicate_upload(self, cleaned_data, instance):
        existing_hashes = set(_contact_image_hashes(instance, self.duplicate_guard_fields))
        for field_name in self.duplicate_guard_fields:
            uploaded = cleaned_data.get(field_name)
            if not uploaded:
                continue
            uploaded_hash = build_contact_image_sha256(uploaded, f"duplicate_guard_{field_name}")
            if uploaded_hash in existing_hashes and not cleaned_data.get(f"allow_duplicate_{field_name}"):
                self.add_error(field_name, "检测到这张图片已上传过。如确认要继续覆盖，请勾选“重复也继续上传”。")


class AgentAdminForm(DuplicateGuardMixin, forms.ModelForm):
    duplicate_guard_fields = ("contact_image_1", "contact_image_2")
    allow_duplicate_contact_image_1 = forms.BooleanField(
        label="图片1重复也继续上传",
        required=False,
        help_text="检测到重复图片时，勾选后继续覆盖。",
    )
    allow_duplicate_contact_image_2 = forms.BooleanField(
        label="图片2重复也继续上传",
        required=False,
        help_text="检测到重复图片时，勾选后继续覆盖。",
    )

    class Meta:
        model = Agent
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_duplicate_guard_attrs(self.instance)

    def clean(self):
        cleaned_data = super().clean()
        self._validate_duplicate_upload(cleaned_data, self.instance)
        return cleaned_data


class SiteContactConfigAdminForm(DuplicateGuardMixin, forms.ModelForm):
    duplicate_guard_fields = ("contact_image_1", "contact_image_2")
    allow_duplicate_contact_image_1 = forms.BooleanField(
        label="官网图片1重复也继续上传",
        required=False,
        help_text="检测到重复图片时，勾选后继续覆盖。",
    )
    allow_duplicate_contact_image_2 = forms.BooleanField(
        label="官网图片2重复也继续上传",
        required=False,
        help_text="检测到重复图片时，勾选后继续覆盖。",
    )

    class Meta:
        model = SiteContactConfig
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_duplicate_guard_attrs(self.instance)

    def clean(self):
        cleaned_data = super().clean()
        self._validate_duplicate_upload(cleaned_data, self.instance)
        return cleaned_data


def _contact_image_preview(image_field):
    if not image_field:
        return "未上传"
    return format_html(
        '<a href="{0}" target="_blank"><img src="{0}" style="max-width:180px;max-height:180px;border-radius:10px;border:1px solid #e5e7eb;padding:4px;background:#fff;" /></a>',
        image_field.url,
    )


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    form = AgentAdminForm
    list_display = (
        "nickname",
        "phone",
        "register_ip",
        "status",
        "public_link",
        "total_paid_orders",
        "total_sales_amount",
        "total_profit_amount",
        "pending_profit_amount",
        "approved_at",
    )
    list_editable = ("status",)
    list_filter = ("status", "created_at", "approved_at")
    search_fields = ("nickname", "phone", "code", "wechat_id", "alipay_account")
    actions = ("approve_selected_agents", "disable_selected_agents", "reset_password_to_phone_suffix")
    readonly_fields = (
        "code",
        "created_at",
        "approved_at",
        "last_login_at",
        "contact_image_1_preview",
        "contact_image_2_preview",
        "agent_summary_block",
        "agent_orders_link",
        "public_link",
    )
    fields = (
        "nickname",
        "phone",
        "register_ip",
        "status",
        "code",
        "contact_qq",
        "contact_wechat",
        "contact_image_1",
        "allow_duplicate_contact_image_1",
        "contact_image_1_preview",
        "contact_image_2",
        "allow_duplicate_contact_image_2",
        "contact_image_2_preview",
        "contact_phone",
        "wechat_id",
        "alipay_account",
        "payee_name",
        "agent_summary_block",
        "agent_orders_link",
        "public_link",
        "created_at",
        "approved_at",
        "last_login_at",
    )
    inlines = (AgentPackagePriceInline,)

    class Media:
        js = ("store/contact-image-duplicate-guard.js",)

    def save_model(self, request, obj, form, change):
        if obj.status == Agent.STATUS_ACTIVE and not obj.approved_at:
            obj.approved_at = timezone.now()
        elif obj.status != Agent.STATUS_ACTIVE:
            obj.approved_at = None
        super().save_model(request, obj, form, change)

    @admin.action(description="审核通过选中代理")
    def approve_selected_agents(self, request, queryset):
        updated = queryset.exclude(status=Agent.STATUS_ACTIVE).update(
            status=Agent.STATUS_ACTIVE,
            approved_at=timezone.now(),
        )
        self.message_user(request, f"已审核通过 {updated} 个代理。")

    @admin.action(description="禁用选中代理")
    def disable_selected_agents(self, request, queryset):
        updated = queryset.exclude(status=Agent.STATUS_DISABLED).update(
            status=Agent.STATUS_DISABLED,
            approved_at=None,
        )
        self.message_user(request, f"已禁用 {updated} 个代理。")

    @admin.action(description="重置所选代理密码为手机号后6位")
    def reset_password_to_phone_suffix(self, request, queryset):
        reset_count = 0
        skipped_count = 0
        for agent in queryset:
            phone_digits = "".join(ch for ch in (agent.phone or "") if ch.isdigit())
            if len(phone_digits) < 6:
                skipped_count += 1
                continue
            agent.set_password(phone_digits[-6:])
            agent.save(update_fields=["password"])
            reset_count += 1

        if reset_count:
            self.message_user(request, f"已重置 {reset_count} 个代理密码，重置后密码为各自手机号后6位。")
        if skipped_count:
            self.message_user(request, f"有 {skipped_count} 个代理因手机号不足 6 位，未执行重置。", level=messages.WARNING)

    @admin.display(description="销售汇总")
    def agent_summary_block(self, obj):
        paid_statuses = [Order.STATUS_PAID, Order.STATUS_ISSUE]
        summary = obj.orders.filter(status__in=paid_statuses).aggregate(
            total_orders=Count("id"),
            total_amount=Sum("amount"),
            total_profit=Sum("agent_profit_snapshot"),
            pending_profit=Sum(
                "agent_profit_snapshot",
                filter=Q(agent_settlement_status=Order.AGENT_SETTLEMENT_PENDING),
            ),
        )
        total_profit = summary["total_profit"] or 0
        pending_profit = summary["pending_profit"] or 0
        settled_profit = total_profit - pending_profit
        return format_html(
            "总订单：{} 单<br>总销售额：¥{}<br>总利润：¥{}<br>待结算：¥{}<br>已结算：¥{}",
            summary["total_orders"] or 0,
            summary["total_amount"] or 0,
            total_profit,
            pending_profit,
            settled_profit,
        )

    @admin.display(description="订单列表")
    def agent_orders_link(self, obj):
        changelist_url = reverse("admin:store_order_changelist")
        return format_html(
            '<a href="{}?agent__id__exact={}">查看这个代理的订单</a>',
            changelist_url,
            obj.pk,
        )

    @admin.display(description="代理链接")
    def public_link(self, obj):
        full_url = f"{settings.SITE_BASE_URL}/a/{obj.code}/"
        return format_html('<a href="{}" target="_blank">{}</a>', full_url, full_url)

    @admin.display(description="总订单")
    def total_paid_orders(self, obj):
        return obj.orders.filter(status__in=[Order.STATUS_PAID, Order.STATUS_ISSUE]).count()

    @admin.display(description="总销售额")
    def total_sales_amount(self, obj):
        return obj.orders.filter(status__in=[Order.STATUS_PAID, Order.STATUS_ISSUE]).aggregate(total=Sum("amount"))["total"] or 0

    @admin.display(description="总利润")
    def total_profit_amount(self, obj):
        return obj.orders.filter(status__in=[Order.STATUS_PAID, Order.STATUS_ISSUE]).aggregate(total=Sum("agent_profit_snapshot"))["total"] or 0

    @admin.display(description="待结算")
    def pending_profit_amount(self, obj):
        return obj.orders.filter(
            status__in=[Order.STATUS_PAID, Order.STATUS_ISSUE],
            agent_settlement_status=Order.AGENT_SETTLEMENT_PENDING,
        ).aggregate(total=Sum("agent_profit_snapshot"))["total"] or 0

    @admin.display(description="联系方式图片1预览")
    def contact_image_1_preview(self, obj):
        return _contact_image_preview(obj.contact_image_1)

    @admin.display(description="联系方式图片2预览")
    def contact_image_2_preview(self, obj):
        return _contact_image_preview(obj.contact_image_2)


@admin.register(SiteContactConfig)
class SiteContactConfigAdmin(admin.ModelAdmin):
    form = SiteContactConfigAdminForm
    list_display = ("title", "updated_at")
    readonly_fields = ("contact_image_1_preview", "contact_image_2_preview", "updated_at")
    fields = (
        "title",
        "contact_image_1",
        "allow_duplicate_contact_image_1",
        "contact_image_1_preview",
        "contact_image_2",
        "allow_duplicate_contact_image_2",
        "contact_image_2_preview",
        "updated_at",
    )

    class Media:
        js = ("store/contact-image-duplicate-guard.js",)

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        if not request.user.is_superuser:
            return False
        if SiteContactConfig.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description="官网图片1预览")
    def contact_image_1_preview(self, obj):
        return _contact_image_preview(obj.contact_image_1)

    @admin.display(description="官网图片2预览")
    def contact_image_2_preview(self, obj):
        return _contact_image_preview(obj.contact_image_2)


@admin.register(MailGatewaySyncConfig)
class MailGatewaySyncConfigAdmin(admin.ModelAdmin):
    list_display = ("title", "auto_sync_on_import", "updated_at")
    list_editable = ("auto_sync_on_import",)
    readonly_fields = ("updated_at",)
    fields = ("title", "auto_sync_on_import", "updated_at")

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        if MailGatewaySyncConfig.objects.exists():
            return False
        return super().has_add_permission(request)


@admin.register(AdminPasswordResetConfig)
class AdminPasswordResetConfigAdmin(admin.ModelAdmin):
    list_display = ("title", "updated_at")
    readonly_fields = ("updated_at",)
    fields = ("title", "reset_emails", "code_expire_minutes", "updated_at")

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        if AdminPasswordResetConfig.objects.exists():
            return False
        return super().has_add_permission(request)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_no",
        "package",
        "agent",
        "quantity",
        "buyer_contact",
        "amount",
        "agent_profit_snapshot",
        "agent_settlement_status",
        "payment_type",
        "status",
        "paid_at",
        "created_at",
    )
    list_filter = ("status", "agent_settlement_status", "agent", "created_at")
    search_fields = ("order_no", "buyer_name", "buyer_contact")
    readonly_fields = ("pickup_password", "delivered_stock_block")
    actions = ("mark_agent_orders_settled",)
    fields = (
        "order_no",
        "package",
        "agent",
        "buyer_name",
        "buyer_contact",
        "pickup_password",
        "quantity",
        "amount",
        "agent_base_price_snapshot",
        "agent_sale_price_snapshot",
        "agent_profit_snapshot",
        "agent_settlement_status",
        "agent_settled_at",
        "payment_type",
        "gateway_trade_no",
        "status",
        "paid_at",
        "created_at",
        "delivered_stock_block",
    )

    @admin.action(description="标记选中订单为代理已结算")
    def mark_agent_orders_settled(self, request, queryset):
        updated = queryset.filter(agent__isnull=False).update(
            agent_settlement_status=Order.AGENT_SETTLEMENT_SETTLED,
            agent_settled_at=timezone.now(),
        )
        self.message_user(request, f"已标记 {updated} 个代理订单为已结算。")

    @admin.display(description="本单发货内容")
    def delivered_stock_block(self, obj):
        items = list(obj.stock_items.all().order_by("id"))
        if items:
            blocks = []
            for index, item in enumerate(items, start=1):
                inbox_line = (
                    format_html("<div><strong>接码网址：</strong>{}</div>", item.inbox_url)
                    if item.inbox_url
                    else ""
                )
                blocks.append(
                    format_html(
                        """
                        <div style="margin-bottom:12px;padding:12px;border:1px solid #e5e7eb;border-radius:8px;background:#fff;">
                            <div style="margin-bottom:6px;"><strong>第 {} 条</strong> / 库存ID：{}</div>
                            {}
                            <pre style="white-space:pre-wrap;word-break:break-all;margin:8px 0 0;padding:10px;background:#f8fafc;border-radius:6px;">{}</pre>
                        </div>
                        """,
                        index,
                        item.pk,
                        inbox_line,
                        item.content,
                    )
                )
            return format_html_join("", "{}", ((block,) for block in blocks))

        if obj.status == Order.STATUS_PENDING:
            return "订单还没支付成功，暂时没有发货内容。"
        if obj.status == Order.STATUS_ISSUE:
            return "订单已支付，但库存分配失败，当前没有实际发货内容。"
        return "当前订单还没有关联到发货内容。"


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "package",
        "content_preview",
        "inbox_url",
        "view_full_link",
        "is_sold",
        "sold_order",
        "sold_at",
        "created_at",
    )
    list_display_links = ("id",)
    list_filter = ("package", "is_sold", "created_at")
    search_fields = ("content",)
    list_per_page = 50
    actions = ("clear_selected_sold_stock",)
    readonly_fields = ("sold_order", "sold_at", "created_at")
    fields = ("package", "content", "inbox_url", "is_sold", "sold_order", "sold_at", "created_at")
    formfield_overrides = {
        django_models.TextField: {
            "widget": forms.Textarea(attrs={"rows": 12, "style": "width: 100%; font-family: monospace;"})
        }
    }

    def changelist_view(self, request, extra_context=None):
        if "is_sold__exact" not in request.GET:
            query = request.GET.copy()
            query["is_sold__exact"] = "0"
            return HttpResponseRedirect(f"{request.path}?{urlencode(query, doseq=True)}")
        return super().changelist_view(request, extra_context=extra_context)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.order_by("-is_sold", "-sold_at", "-id")

    @admin.display(description="内容预览")
    def content_preview(self, obj):
        return Truncator(obj.content).chars(48)

    @admin.display(description="完整内容")
    def view_full_link(self, obj):
        return format_html(
            '<a href="{}">查看完整</a>',
            reverse("admin:store_stockitem_change", args=[obj.pk]),
        )

    @admin.action(description="只删除选中的已售库存")
    def clear_selected_sold_stock(self, request, queryset):
        sold_queryset = queryset.filter(is_sold=True)
        deleted_count, _ = sold_queryset.delete()
        skipped_count = queryset.count() - deleted_count
        if deleted_count:
            self.message_user(request, f"已删除 {deleted_count} 条已售库存。")
        if skipped_count:
            self.message_user(request, f"已跳过 {skipped_count} 条未售库存。")


admin.site.unregister(Group)
admin.site.unregister(User)
