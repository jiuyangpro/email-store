from decimal import Decimal
from datetime import timedelta
from io import BytesIO, StringIO
from tempfile import TemporaryDirectory
from django.core import mail
from django.core.management import call_command
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.hashers import make_password
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import RequestFactory
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from importlib import reload
from PIL import Image
from unittest.mock import patch

from .admin import AgentAdmin, OrderAdmin, PackageAdmin, PackageAdminForm, StockItemAdmin, StockItemInline, _build_pending_imports, _split_group_blocks
from .ckkp import build_sign_content, sign_payload, verify_payload
from .models import Agent, AgentEmailVerification, AgentPackagePrice, Document, Order, Package, SiteContactConfig, StockItem
from .views import AGENT_SESSION_KEY, _build_delivery_copy_text, _mark_order_paid
from config import settings as project_settings


def make_test_image(name="contact.png", size=(1800, 1200), color=(20, 120, 220)):
    output = BytesIO()
    image = Image.new("RGB", size, color)
    image.save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


def create_agent_email_code(email, purpose, code="123456", agent=None):
    return AgentEmailVerification.objects.create(
        agent=agent,
        email=email,
        purpose=purpose,
        code=code,
        expires_at=timezone.now() + timedelta(minutes=10),
    )


class StoreFlowTests(TestCase):
    def setUp(self):
        document = Document.objects.create(title="测试文档", summary="测试简介")
        self.package = Package.objects.create(
            name="测试套餐",
            subtitle="测试副标题",
            description="测试说明",
            price="19.90",
            original_price="29.90",
        )
        self.package.documents.add(document)
        self.line_package = Package.objects.create(
            name="按条测试",
            subtitle="按条发货",
            description="按条购买",
            price="0.40",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        self.group_package = Package.objects.create(
            name="按组测试",
            subtitle="按组发货",
            description="按组购买",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
        )
        self.line_package.agent_floor_price = "0.30"
        self.line_package.save(update_fields=["agent_floor_price"])
        self.group_package.agent_floor_price = "25.00"
        self.group_package.save(update_fields=["agent_floor_price"])


    def test_home_page_loads(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "按条测试")
        self.assertContains(response, "按组测试")
        self.assertContains(response, "企业邮箱购买")
        self.assertContains(response, "FAQPage")

    def test_robots_txt_exposes_sitemap(self):
        response = self.client.get(reverse("robots_txt"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sitemap:")
        self.assertContains(response, "/sitemap.xml")

    def test_sitemap_xml_lists_home_and_public_packages(self):
        response = self.client.get(reverse("sitemap_xml"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<urlset", html=False)
        self.assertContains(response, "/packages/", html=False)
        self.assertContains(response, "/google-mail/", html=False)
        self.assertContains(response, "/enterprise-google-mail/", html=False)

    def test_seo_landing_pages_load(self):
        response = self.client.get(reverse("seo_google_mail"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "企业邮箱购买")
        self.assertContains(response, "按条购买")

    def test_private_pages_are_noindex(self):
        order = Order.objects.create(
            order_no="ORDER-NOINDEX",
            package=self.package,
            buyer_contact="wx-noindex",
            pickup_password=make_password("abc123"),
            amount="19.90",
        )

        order_response = self.client.get(reverse("order_detail", args=[order.pk]))
        pickup_response = self.client.get(reverse("pickup_lookup"))

        self.assertContains(order_response, 'content="noindex,nofollow,noarchive"', html=False)
        self.assertContains(pickup_response, 'content="noindex,nofollow,noarchive"', html=False)

    def test_admin_index_shows_today_sales_summary(self):
        admin_user = get_user_model().objects.create_superuser(
            username="adminsales",
            email="adminsales@example.com",
            password="admin123456",
        )
        paid_order = Order.objects.create(
            order_no="ORDER-TODAY-SALES",
            package=self.line_package,
            buyer_contact="wx-sales",
            pickup_password=make_password("abc123"),
            quantity=3,
            amount="90.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )

        self.client.force_login(admin_user)
        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "今日销售额")
        self.assertEqual(response.context["sales_summary"]["today_paid_order_count"], 1)
        self.assertEqual(response.context["sales_summary"]["today_total_quantity"], paid_order.quantity)

    def test_build_pending_imports_treats_line_text_as_single_group_for_group_mode(self):
        lines, groups = _build_pending_imports(
            stock_mode=Package.STOCK_GROUP,
            bulk_text="a@x.com----1----2fa\nb@x.com----2----2fa",
            bulk_groups="",
        )

        self.assertEqual(lines, [])
        self.assertEqual(groups, ["a@x.com----1----2fa\nb@x.com----2----2fa"])

    def test_build_pending_imports_flattens_group_input_for_line_mode(self):
        lines, groups = _build_pending_imports(
            stock_mode=Package.STOCK_LINE,
            bulk_text="",
            bulk_groups="a@x.com----1----2fa\nb@x.com----2----2fa",
        )

        self.assertEqual(
            lines,
            ["a@x.com----1----2fa", "b@x.com----2----2fa"],
        )
        self.assertEqual(groups, [])

    def test_home_prefers_line_package_with_available_stock(self):
        empty_line = Package.objects.create(
            name="按条空库存",
            subtitle="空",
            description="空库存",
            price="0.40",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        stocked_line = Package.objects.create(
            name="按条有库存",
            subtitle="有货",
            description="有库存",
            price="0.40",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        StockItem.objects.create(package=stocked_line, content="账号1----密码1----2fa1")
        StockItem.objects.create(package=stocked_line, content="账号2----密码2----2fa2")

        response = self.client.get(reverse("home"))

        self.assertEqual(response.context["line_package"].pk, stocked_line.pk)
        self.assertNotEqual(response.context["line_package"].pk, empty_line.pk)

    def test_create_order_redirects_to_order_page(self):
        response = self.client.post(
            reverse("create_order", args=[self.package.pk]),
            {"buyer_name": "张三", "buyer_contact": "wx-001", "pickup_password": "abc123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 1)

    @override_settings(CKKP_SIGN_TYPE="MD5", CKKP_MD5_KEY="")
    def test_order_detail_shows_payment_notice_when_key_missing(self):
        order = Order.objects.create(
            order_no="ORDER001",
            package=self.package,
            buyer_contact="wx-001",
            pickup_password=make_password("abc123"),
            amount="19.90",
        )

        response = self.client.get(reverse("order_detail", args=[order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "当前暂时无法发起支付，请稍后再试。")

    @override_settings(CKKP_SIGN_TYPE="MD5", CKKP_MD5_KEY="testkey", CKKP_PID="1002", CKKP_TYPE="")
    def test_start_payment_uses_cashier_when_type_is_blank(self):
        order = Order.objects.create(
            order_no="ORDER-CASHIER",
            package=self.package,
            buyer_contact="wx-001",
            pickup_password=make_password("abc123"),
            amount="19.90",
        )

        response = self.client.get(reverse("start_payment", args=[order.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("type", response.context["payment_data"])
        self.assertEqual(response.context["payment_data"]["sign_type"], "MD5")

    def test_start_payment_rejects_issue_order(self):
        order = Order.objects.create(
            order_no="ORDER-ISSUE-PAY",
            package=self.package,
            buyer_contact="wx-issue-pay",
            pickup_password=make_password("abc123"),
            amount="19.90",
            status=Order.STATUS_ISSUE,
        )

        response = self.client.get(reverse("start_payment", args=[order.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "当前正在人工处理")

    def test_start_payment_rejects_closed_order(self):
        order = Order.objects.create(
            order_no="ORDER-CLOSED-PAY",
            package=self.package,
            buyer_contact="wx-closed-pay",
            pickup_password=make_password("abc123"),
            amount="19.90",
            status=Order.STATUS_CLOSED,
        )

        response = self.client.get(reverse("start_payment", args=[order.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "订单已关闭")

    @override_settings(CKKP_SIGN_TYPE="MD5", CKKP_MD5_KEY="testkey", CKKP_PID="1002")
    def test_ckkp_notify_rejects_mismatched_pid(self):
        order = Order.objects.create(
            order_no="ORDER-NOTIFY-PID",
            package=self.package,
            buyer_contact="wx-notify-pid",
            pickup_password=make_password("abc123"),
            amount="19.90",
        )
        payload = {
            "pid": "9999",
            "trade_no": "CK-NOTIFY-001",
            "out_trade_no": order.order_no,
            "type": "alipay",
            "name": order.package.name,
            "money": "19.90",
            "trade_status": "TRADE_SUCCESS",
            "sign_type": "MD5",
        }
        payload["sign"] = sign_payload(payload, sign_type="MD5", md5_key="testkey")

        response = self.client.get(reverse("ckkp_notify"), payload)
        order.refresh_from_db()

        self.assertEqual(response.content.decode("utf-8"), "fail")
        self.assertEqual(order.status, Order.STATUS_PENDING)

    @override_settings(CKKP_SIGN_TYPE="MD5", CKKP_MD5_KEY="testkey", CKKP_PID="1002")
    def test_ckkp_return_rejects_mismatched_pid(self):
        order = Order.objects.create(
            order_no="ORDER-RETURN-PID",
            package=self.package,
            buyer_contact="wx-return-pid",
            pickup_password=make_password("abc123"),
            amount="19.90",
        )
        payload = {
            "pid": "9999",
            "trade_no": "CK-RETURN-001",
            "out_trade_no": order.order_no,
            "type": "alipay",
            "name": order.package.name,
            "money": "19.90",
            "trade_status": "TRADE_SUCCESS",
            "sign_type": "MD5",
        }
        payload["sign"] = sign_payload(payload, sign_type="MD5", md5_key="testkey")

        response = self.client.get(reverse("ckkp_return"), payload, follow=True)
        order.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "商户信息不匹配")
        self.assertEqual(order.status, Order.STATUS_PENDING)

    def test_create_stock_order_with_quantity(self):
        stock_package = Package.objects.create(
            name="卡密商品",
            subtitle="按条发货",
            description="买几条发几条",
            price="5.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        for idx in range(3):
            StockItem.objects.create(package=stock_package, content=f"卡密-{idx}")

        response = self.client.post(
            reverse("create_order", args=[stock_package.pk]),
            {"buyer_contact": "wx-001", "pickup_password": "abc123", "quantity": "2"},
        )

        order = Order.objects.latest("id")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(order.quantity, 2)
        self.assertEqual(str(order.amount), "10.00")

    def test_paid_stock_order_allocates_requested_quantity(self):
        stock_package = Package.objects.create(
            name="卡密商品",
            subtitle="按条发货",
            description="买几条发几条",
            price="5.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        for idx in range(5):
            StockItem.objects.create(package=stock_package, content=f"卡密-{idx}")

        order = Order.objects.create(
            order_no="ORDER-STOCK",
            package=stock_package,
            buyer_contact="wx-001",
            pickup_password=make_password("abc123"),
            quantity=2,
            amount="10.00",
        )

        _mark_order_paid(order, {"trade_no": "T001", "type": "alipay"})
        order.refresh_from_db()

        self.assertEqual(order.status, Order.STATUS_PAID)
        self.assertEqual(order.stock_items.count(), 2)
        self.assertEqual(stock_package.available_stock_count, 3)

    def test_paid_group_order_allocates_whole_groups(self):
        group_package = Package.objects.create(
            name="组货商品",
            subtitle="按组发货",
            description="买几组发几组",
            price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
        )
        StockItem.objects.create(package=group_package, content="A1\nA2\nA3")
        StockItem.objects.create(package=group_package, content="B1\nB2")
        StockItem.objects.create(package=group_package, content="C1\nC2\nC3")

        order = Order.objects.create(
            order_no="ORDER-GROUP",
            package=group_package,
            buyer_contact="wx-001",
            pickup_password=make_password("abc123"),
            quantity=2,
            amount="60.00",
        )

        _mark_order_paid(order, {"trade_no": "T002", "type": "alipay"})
        order.refresh_from_db()

        self.assertEqual(order.status, Order.STATUS_PAID)
        self.assertEqual(order.stock_items.count(), 2)
        self.assertEqual(group_package.available_stock_count, 1)

    def test_paid_order_becomes_issue_when_stock_is_insufficient(self):
        stock_package = Package.objects.create(
            name="库存不足商品",
            subtitle="按条发货",
            description="库存不足测试",
            price="5.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        StockItem.objects.create(package=stock_package, content="卡密-1")

        order = Order.objects.create(
            order_no="ORDER-ISSUE",
            package=stock_package,
            buyer_contact="wx-issue",
            pickup_password=make_password("abc123"),
            quantity=2,
            amount="10.00",
        )

        result_status = _mark_order_paid(order, {"trade_no": "T003", "type": "alipay"})
        order.refresh_from_db()

        self.assertEqual(result_status, Order.STATUS_ISSUE)
        self.assertEqual(order.status, Order.STATUS_ISSUE)
        self.assertEqual(order.stock_items.count(), 0)
        self.assertEqual(stock_package.available_stock_count, 1)

    def test_pickup_page_unlocks_with_correct_password(self):
        stock_package = Package.objects.create(
            name="卡密商品",
            subtitle="按条发货",
            description="买几条发几条",
            price="5.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        order = Order.objects.create(
            order_no="ORDER-PICK",
            package=stock_package,
            buyer_contact="qq-001",
            pickup_password=make_password("mypwd"),
            quantity=1,
            amount="5.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )
        StockItem.objects.create(
            package=stock_package,
            content="账号----密码----密钥",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now(),
        )

        response = self.client.post(
            reverse("pickup_order", args=[order.pk]),
            {"pickup_password": "mypwd"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "账号----密码----密钥")

    def test_build_delivery_copy_text_for_line_order_appends_single_instruction_block(self):
        package = Package.objects.create(
            name="按条复制测试",
            subtitle="按条",
            description="测试",
            price="5.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        first = StockItem.objects.create(package=package, content="line1@example.com----pwd1")
        second = StockItem.objects.create(package=package, content="line2@example.com----pwd2")

        copy_text = _build_delivery_copy_text(package, [first, second])

        self.assertIn("line1@example.com----pwd1", copy_text)
        self.assertIn("line2@example.com----pwd2", copy_text)
        self.assertIn("接码网址：https://fbvjdjcf.asia", copy_text)
        self.assertIn("登录密码：123456", copy_text)
        self.assertEqual(copy_text.count("邮箱不要泄露。"), 1)

    def test_build_delivery_copy_text_for_group_order_appends_single_instruction_block(self):
        package = Package.objects.create(
            name="按组复制测试",
            subtitle="按组",
            description="测试",
            price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
        )
        group_item = StockItem.objects.create(package=package, content="admin@example.com----pwd\nmember@example.com----pwd")

        copy_text = _build_delivery_copy_text(package, [group_item])

        self.assertIn("admin@example.com----pwd", copy_text)
        self.assertNotIn("后台网址：admin.google.com", copy_text)
        self.assertIn("接码网址：https://fbvjdjcf.asia", copy_text)
        self.assertIn("验证码登录：输入需要收验证码的邮箱，密码 123456。", copy_text)
        self.assertEqual(copy_text.count("邮箱不要泄露。"), 1)

    def test_build_delivery_copy_text_for_group_order_uses_each_item_inbox_url_when_mixed(self):
        package = Package.objects.create(
            name="按组混合网址测试",
            subtitle="按组",
            description="测试",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
        )
        first = StockItem.objects.create(
            package=package,
            content="a@example.com----pwd",
            inbox_url="https://fbvjdjcf.asia",
        )
        second = StockItem.objects.create(
            package=package,
            content="b@example.com----pwd",
            inbox_url="https://43.134.2.194/roundcube邮箱验证网站",
        )

        copy_text = _build_delivery_copy_text(package, [first, second], include_labels=True)

        self.assertIn("接码网址：https://fbvjdjcf.asia", copy_text)
        self.assertIn("接码网址：https://43.134.2.194/roundcube邮箱验证网站", copy_text)
        self.assertEqual(copy_text.count("验证码登录：输入需要收验证码的邮箱，密码 123456。"), 2)

    def test_issue_order_cannot_be_picked_up(self):
        stock_package = Package.objects.create(
            name="异常商品",
            subtitle="按条发货",
            description="异常测试",
            price="5.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        order = Order.objects.create(
            order_no="ORDER-LOCK",
            package=stock_package,
            buyer_contact="qq-issue",
            pickup_password=make_password("mypwd"),
            quantity=1,
            amount="5.00",
            status=Order.STATUS_ISSUE,
            paid_at=timezone.now(),
        )

        response = self.client.post(
            reverse("pickup_order", args=[order.pk]),
            {"pickup_password": "mypwd"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "订单已支付，但当前正在人工处理发货")

    def test_pickup_lookup_reports_issue_order(self):
        stock_package = Package.objects.create(
            name="查询异常商品",
            subtitle="按条发货",
            description="异常查询测试",
            price="5.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        Order.objects.create(
            order_no="ORDER-LOOKUP-ISSUE",
            package=stock_package,
            buyer_contact="qq-lookup-issue",
            pickup_password=make_password("mypwd"),
            quantity=1,
            amount="5.00",
            status=Order.STATUS_ISSUE,
            paid_at=timezone.now(),
        )

        response = self.client.post(
            reverse("pickup_lookup"),
            {"buyer_contact": "qq-lookup-issue", "pickup_password": "mypwd"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "订单正在人工处理")

    def test_pickup_password_fields_are_masked(self):
        order = Order.objects.create(
            order_no="ORDER-MASKED-PASSWORD",
            package=self.package,
            buyer_contact="wx-masked",
            pickup_password=make_password("mypwd"),
            amount="19.90",
        )

        order_response = self.client.get(reverse("pickup_order", args=[order.pk]))
        lookup_response = self.client.get(reverse("pickup_lookup"))

        self.assertContains(order_response, 'type="password"', html=False)
        self.assertContains(lookup_response, 'type="password"', html=False)

    def test_package_detail_pickup_password_field_is_masked(self):
        response = self.client.get(reverse("package_detail", args=[self.package.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pickup_password"', html=False)
        self.assertContains(response, 'type="password"', html=False)


class CkkpUtilsTests(TestCase):
    def test_build_sign_content_sorts_and_skips_empty_values(self):
        payload = {
            "money": "1.00",
            "pid": "1002",
            "sign": "ignored",
            "name": "测试商品",
            "param": "",
            "sign_type": "RSA",
        }

        self.assertEqual(
            build_sign_content(payload),
            "money=1.00&name=测试商品&pid=1002",
        )

    def test_md5_sign_payload(self):
        payload = {
            "pid": "1002",
            "type": "alipay",
            "out_trade_no": "ORDER001",
            "name": "测试商品",
            "money": "1.00",
            "sign_type": "MD5",
        }

        sign = sign_payload(payload, sign_type="MD5", md5_key="testkey")

        self.assertEqual(sign, "fc1b421507e69e04a9ac1e4d853cff99")

    def test_md5_verify_payload(self):
        payload = {
            "pid": "1002",
            "trade_no": "CK001",
            "out_trade_no": "ORDER001",
            "type": "alipay",
            "name": "测试商品",
            "money": "1.00",
            "trade_status": "TRADE_SUCCESS",
            "sign_type": "MD5",
        }
        payload["sign"] = sign_payload(payload, sign_type="MD5", md5_key="testkey")

        self.assertTrue(verify_payload(payload, sign_type="MD5", md5_key="testkey"))


class AdminImportTests(TestCase):
    def test_split_group_blocks_supports_group_separator(self):
        text = "====GROUP====\na1\nb1\n====GROUP====\nc1\nc2"

        groups = _split_group_blocks(text)

        self.assertEqual(groups, ["a1\nb1", "c1\nc2"])

    def test_split_group_blocks_supports_single_blank_line_separator(self):
        text = "a1\nb1\n\nc1\nc2"

        groups = _split_group_blocks(text)

        self.assertEqual(groups, ["a1\nb1", "c1\nc2"])

    def test_package_admin_form_rejects_second_active_group_package(self):
        Package.objects.create(
            name="正式按组商品",
            subtitle="按组",
            description="正式按组",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        form = PackageAdminForm(
            data={
                "name": "重复按组商品",
                "subtitle": "按组",
                "description": "重复按组",
                "price": "30.00",
                "original_price": "30.00",
                "stock_mode": Package.STOCK_GROUP,
                "is_active": True,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("按组售卖 只能保留 1 个上架商品", form.non_field_errors()[0])

    def test_package_admin_form_allows_switch_after_old_group_package_is_inactive(self):
        Package.objects.create(
            name="旧按组商品",
            subtitle="按组",
            description="旧按组",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=False,
        )
        form = PackageAdminForm(
            data={
                "name": "新按组商品",
                "subtitle": "按组",
                "description": "新按组",
                "price": "30.00",
                "original_price": "30.00",
                "stock_mode": Package.STOCK_GROUP,
                "is_active": True,
            }
        )

        self.assertTrue(form.is_valid())

    def test_package_admin_form_routes_import_into_existing_active_group_package(self):
        existing = Package.objects.create(
            name="正式按组商品",
            subtitle="按组",
            description="正式按组",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        upload = SimpleUploadedFile(
            "groups.txt",
            "====GROUP====\na1\nb1\n====GROUP====\nc1\nc2\n".encode("utf-8"),
            content_type="text/plain",
        )
        form = PackageAdminForm(
            data={
                "name": "新按组商品",
                "subtitle": "按组",
                "description": "测试",
                "price": "30.00",
                "original_price": "30.00",
                "stock_mode": Package.STOCK_GROUP,
                "is_active": "on",
                "bulk_import_text": "",
                "bulk_import_groups": "",
            },
            files={"bulk_import_groups_file": upload},
        )

        self.assertTrue(form.is_valid(), form.errors)
        package = form.save()
        self.assertEqual(package.pk, existing.pk)
        self.assertEqual(Package.objects.count(), 1)
        self.assertEqual(existing.stock_items.count(), 2)

    def test_admin_add_view_routes_group_import_into_existing_package_without_500(self):
        existing = Package.objects.create(
            name="正式按组商品",
            subtitle="按组",
            description="正式按组",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        admin_user = get_user_model().objects.create_superuser(
            username="uploadadmin",
            email="uploadadmin@example.com",
            password="admin123456",
        )
        upload = SimpleUploadedFile(
            "groups.txt",
            "====GROUP====\na1\nb1\n====GROUP====\nc1\nc2\n".encode("utf-8"),
            content_type="text/plain",
        )

        self.client.force_login(admin_user)
        response = self.client.post(
            reverse("admin:store_package_add"),
            data={
                "name": "新增页按组补货",
                "subtitle": "按组",
                "description": "测试",
                "price": "30.00",
                "original_price": "30.00",
                "stock_mode": Package.STOCK_GROUP,
                "is_active": "on",
                "bulk_import_text": "",
                "bulk_import_groups": "",
                "bulk_import_groups_file": upload,
                "_save": "保存",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Package.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.stock_items.count(), 2)

    def test_package_admin_merge_unsold_stock_into_primary(self):
        primary = Package.objects.create(
            name="按组正式商品",
            subtitle="按组",
            description="正式按组",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        duplicate = Package.objects.create(
            name="按组重复商品",
            subtitle="按组",
            description="重复按组",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        order = Order.objects.create(
            order_no="ORDER-MERGE-GROUP",
            package=duplicate,
            buyer_contact="wx-merge-group",
            pickup_password=make_password("123456"),
            amount="30.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )
        primary_unsold = StockItem.objects.create(package=primary, content="group-a")
        moved_unsold = StockItem.objects.create(package=duplicate, content="group-b")
        sold_item = StockItem.objects.create(
            package=duplicate,
            content="group-sold",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now(),
        )
        request = RequestFactory().post("/admin/store/package/")
        setattr(request, "session", self.client.session)
        messages = FallbackStorage(request)
        setattr(request, "_messages", messages)
        request.user = get_user_model().objects.create_superuser(
            username="mergeadmin",
            email="mergeadmin@example.com",
            password="admin123456",
        )

        admin_obj = PackageAdmin(Package, AdminSite())
        admin_obj.merge_unsold_stock_into_primary(request, Package.objects.filter(pk__in=[primary.pk, duplicate.pk]))

        primary.refresh_from_db()
        duplicate.refresh_from_db()
        moved_unsold.refresh_from_db()
        sold_item.refresh_from_db()

        self.assertEqual(primary.stock_items.filter(is_sold=False).count(), 2)
        self.assertEqual(moved_unsold.package_id, primary.pk)
        self.assertEqual(primary_unsold.package_id, primary.pk)
        self.assertEqual(sold_item.package_id, duplicate.pk)
        self.assertFalse(duplicate.is_active)

    def test_package_admin_merge_unsold_stock_rejects_mixed_modes(self):
        line_package = Package.objects.create(
            name="正式按条商品",
            subtitle="按条",
            description="正式按条",
            price="0.40",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
            is_active=True,
        )
        group_package = Package.objects.create(
            name="正式按组商品",
            subtitle="按组",
            description="正式按组",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        request = RequestFactory().post("/admin/store/package/")
        setattr(request, "session", self.client.session)
        messages = FallbackStorage(request)
        setattr(request, "_messages", messages)
        request.user = get_user_model().objects.create_superuser(
            username="mergemixedadmin",
            email="mergemixedadmin@example.com",
            password="admin123456",
        )

        admin_obj = PackageAdmin(Package, AdminSite())
        admin_obj.merge_unsold_stock_into_primary(request, Package.objects.filter(pk__in=[line_package.pk, group_package.pk]))

        line_package.refresh_from_db()
        group_package.refresh_from_db()
        self.assertTrue(line_package.is_active)
        self.assertTrue(group_package.is_active)

    def test_package_admin_has_no_stock_item_inline(self):
        request = RequestFactory().get("/admin/store/package/add/")
        request.user = get_user_model().objects.create_superuser(
            username="packageinlineadmin",
            email="packageinlineadmin@example.com",
            password="admin123456",
        )
        admin_obj = PackageAdmin(Package, AdminSite())

        self.assertEqual(admin_obj.get_inline_instances(request), [])

    def test_package_admin_stock_view_link_defaults_to_unsold_filter(self):
        package = Package.objects.create(
            name="库存链接商品",
            subtitle="按组",
            description="测试",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        admin_obj = PackageAdmin(Package, AdminSite())
        html = admin_obj.stock_view_link(package)

        self.assertIn(f"package__id__exact={package.pk}", html)
        self.assertIn("is_sold__exact=0", html)
        self.assertIn("is_sold__exact=1", html)

    def test_package_admin_queryset_hides_inactive_stock_packages_without_unsold_inventory(self):
        active_group = Package.objects.create(
            name="正式按组商品",
            subtitle="按组",
            description="测试",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        hidden_duplicate = Package.objects.create(
            name="重复按组商品",
            subtitle="按组",
            description="测试",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=False,
        )
        request = RequestFactory().get("/admin/store/package/")
        request.user = get_user_model().objects.create_superuser(
            username="packagequeryadmin",
            email="packagequeryadmin@example.com",
            password="admin123456",
        )
        admin_obj = PackageAdmin(Package, AdminSite())
        ids = list(admin_obj.get_queryset(request).values_list("id", flat=True))

        self.assertIn(active_group.pk, ids)
        self.assertNotIn(hidden_duplicate.pk, ids)

    def test_stock_item_admin_changelist_defaults_to_unsold_filter(self):
        request = RequestFactory().get("/admin/store/stockitem/")
        request.user = get_user_model().objects.create_superuser(
            username="stocklistadmin",
            email="stocklistadmin@example.com",
            password="admin123456",
        )
        admin_obj = StockItemAdmin(StockItem, AdminSite())
        response = admin_obj.changelist_view(request)

        self.assertIsInstance(response, HttpResponseRedirect)
        self.assertIn("is_sold__exact=0", response.url)

    def test_line_txt_file_upload_imports_stock_items(self):
        upload = SimpleUploadedFile(
            "lines.txt",
            "a----1----x\nb----2----y\n".encode("utf-8"),
            content_type="text/plain",
        )
        form = PackageAdminForm(
            data={
                "name": "按条上传商品",
                "subtitle": "按条",
                "description": "测试",
                "price": "0.40",
                "original_price": "0.40",
                "stock_mode": Package.STOCK_LINE,
                "is_active": "on",
                "bulk_import_text": "",
                "bulk_import_groups": "",
            },
            files={"bulk_import_text_file": upload},
        )

        self.assertTrue(form.is_valid(), form.errors)
        package = form.save()
        self.assertEqual(package.stock_items.count(), 2)

    def test_package_admin_form_blocks_duplicate_line_stock_until_confirmed(self):
        package = Package.objects.create(
            name="重复按条商品",
            subtitle="按条",
            description="测试",
            price="0.40",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
            is_active=True,
        )
        StockItem.objects.create(package=package, content="a----1----x")

        form = PackageAdminForm(
            instance=package,
            data={
                "name": "重复按条商品",
                "subtitle": "按条",
                "description": "测试",
                "price": "0.40",
                "original_price": "0.40",
                "stock_mode": Package.STOCK_LINE,
                "is_active": "on",
                "bulk_import_text": "a----1----x\nb----2----y",
                "bulk_import_groups": "",
                "allow_duplicate_import": "",
            },
        )

        self.assertFalse(form.is_valid())
        self.assertIn("已存在", form.errors["allow_duplicate_import"][0])

    def test_package_admin_form_allows_duplicate_line_stock_after_confirm(self):
        package = Package.objects.create(
            name="重复确认按条商品",
            subtitle="按条",
            description="测试",
            price="0.40",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
            is_active=True,
        )
        StockItem.objects.create(package=package, content="a----1----x")

        form = PackageAdminForm(
            instance=package,
            data={
                "name": "重复确认按条商品",
                "subtitle": "按条",
                "description": "测试",
                "price": "0.40",
                "original_price": "0.40",
                "stock_mode": Package.STOCK_LINE,
                "is_active": "on",
                "bulk_import_text": "a----1----x\nb----2----y",
                "bulk_import_groups": "",
                "allow_duplicate_import": "on",
            },
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(package.stock_items.count(), 3)

    def test_group_txt_file_upload_imports_group_stock(self):
        upload = SimpleUploadedFile(
            "groups.txt",
            "====GROUP====\na1\nb1\n====GROUP====\nc1\nc2\n".encode("utf-8"),
            content_type="text/plain",
        )
        form = PackageAdminForm(
            data={
                "name": "按组上传商品",
                "subtitle": "按组",
                "description": "测试",
                "price": "30.00",
                "original_price": "30.00",
                "stock_mode": Package.STOCK_GROUP,
                "is_active": "on",
                "bulk_import_text": "",
                "bulk_import_groups": "",
            },
            files={"bulk_import_groups_file": upload},
        )

        self.assertTrue(form.is_valid(), form.errors)
        package = form.save()
        self.assertEqual(package.stock_items.count(), 2)
        self.assertEqual(package.stock_items.first().inbox_url, "https://fbvjdjcf.asia")

    def test_group_second_port_imports_group_stock_with_second_inbox_url(self):
        upload = SimpleUploadedFile(
            "groups2.txt",
            "====GROUP====\na2\nb2\n====GROUP====\nc2\nc3\n".encode("utf-8"),
            content_type="text/plain",
        )
        form = PackageAdminForm(
            data={
                "name": "按组上传商品2",
                "subtitle": "按组",
                "description": "测试",
                "price": "30.00",
                "original_price": "30.00",
                "stock_mode": Package.STOCK_GROUP,
                "is_active": "on",
                "bulk_import_text": "",
                "bulk_import_groups": "",
                "bulk_import_groups_port2": "",
                "bulk_import_groups_url": "https://fbvjdjcf.asia",
                "bulk_import_groups_url_port2": "https://43.134.2.194/roundcube邮箱验证网站",
            },
            files={"bulk_import_groups_file_port2": upload},
        )

        self.assertTrue(form.is_valid(), form.errors)
        package = form.save()
        self.assertEqual(package.stock_items.count(), 2)
        self.assertEqual(
            list(package.stock_items.values_list("inbox_url", flat=True)),
            ["https://43.134.2.194/roundcube邮箱验证网站"] * 2,
        )

    def test_package_admin_flow_imports_stock_items_after_save_model(self):
        upload = SimpleUploadedFile(
            "lines.txt",
            "x----1----a\ny----2----b\n".encode("utf-8"),
            content_type="text/plain",
        )
        form = PackageAdminForm(
            data={
                "name": "后台导入商品",
                "subtitle": "按条",
                "description": "测试",
                "price": "0.40",
                "original_price": "0.40",
                "stock_mode": Package.STOCK_LINE,
                "is_active": "on",
                "bulk_import_text": "",
                "bulk_import_groups": "",
            },
            files={"bulk_import_text_file": upload},
        )
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        obj.save()

        request = RequestFactory().post("/admin/store/package/add/")
        request.session = self.client.session
        setattr(request, "_messages", FallbackStorage(request))
        admin_obj = PackageAdmin(Package, AdminSite())
        admin_obj.save_model(request, obj, form, change=False)

        self.assertEqual(obj.stock_items.count(), 2)

    def test_package_admin_clear_sold_stock_only_deletes_sold_items(self):
        package = Package.objects.create(
            name="清理测试商品",
            subtitle="按条",
            description="测试",
            price="1.00",
            original_price="1.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        order = Order.objects.create(
            order_no="ORDER-ADMIN-CLEAR",
            package=package,
            buyer_contact="wx-clear",
            pickup_password=make_password("123456"),
            amount="1.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )
        sold_item = StockItem.objects.create(
            package=package,
            content="sold-item",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now(),
        )
        unsold_item = StockItem.objects.create(package=package, content="unsold-item")

        request = RequestFactory().post("/admin/store/package/")
        request.session = self.client.session
        setattr(request, "_messages", FallbackStorage(request))
        admin_obj = PackageAdmin(Package, AdminSite())
        admin_obj.clear_sold_stock(request, Package.objects.filter(pk=package.pk))

        self.assertFalse(StockItem.objects.filter(pk=sold_item.pk).exists())
        self.assertTrue(StockItem.objects.filter(pk=unsold_item.pk).exists())

    @override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
    def test_admin_changelist_price_edit_does_not_raise_500(self):
        admin_user = get_user_model().objects.create_superuser(
            username="priceeditadmin",
            email="priceeditadmin@example.com",
            password="admin123456",
        )
        line_package = Package.objects.create(
            name="后台改价按条",
            subtitle="按条",
            description="测试",
            price="0.40",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
            is_active=True,
        )
        group_package = Package.objects.create(
            name="后台改价按组",
            subtitle="按组",
            description="测试",
            price="30.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
            is_active=True,
        )
        client = self.client_class()
        client.force_login(admin_user)
        packages = [line_package, group_package]
        post = {
            "form-TOTAL_FORMS": "2",
            "form-INITIAL_FORMS": "2",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        for index, package in enumerate(packages):
            post[f"form-{index}-id"] = str(package.pk)
            post[f"form-{index}-name"] = package.name
            post[f"form-{index}-price"] = str(package.price)
            post[f"form-{index}-original_price"] = str(package.original_price)
            if package.is_active:
                post[f"form-{index}-is_active"] = "on"
        post["form-0-price"] = "0.41"

        response = client.post("/admin/store/package/", post)

        self.assertEqual(response.status_code, 302)
        line_package.refresh_from_db()
        self.assertEqual(line_package.price, Decimal("0.41"))

    def test_cleanup_expired_sold_stock_keeps_orders_and_recent_stock(self):
        package = Package.objects.create(
            name="自动清理商品",
            subtitle="按条",
            description="测试",
            price="1.00",
            original_price="1.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        order = Order.objects.create(
            order_no="ORDER-AUTO-CLEAN",
            package=package,
            buyer_contact="wx-auto-clean",
            pickup_password=make_password("123456"),
            amount="88.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now() - timedelta(days=8),
        )
        expired_sold = StockItem.objects.create(
            package=package,
            content="expired-sold",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now() - timedelta(days=7, minutes=1),
        )
        recent_sold = StockItem.objects.create(
            package=package,
            content="recent-sold",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now() - timedelta(days=6, hours=23),
        )
        unsold_item = StockItem.objects.create(package=package, content="unsold-item")
        stdout = StringIO()

        call_command("cleanup_expired_sold_stock", stdout=stdout)

        self.assertFalse(StockItem.objects.filter(pk=expired_sold.pk).exists())
        self.assertTrue(StockItem.objects.filter(pk=recent_sold.pk).exists())
        self.assertTrue(StockItem.objects.filter(pk=unsold_item.pk).exists())
        self.assertTrue(Order.objects.filter(pk=order.pk, amount="88.00").exists())
        self.assertIn("订单与金额记录已保留", stdout.getvalue())

    def test_agent_public_page_uses_agent_price_without_contact_block(self):
        line_package = Package.objects.create(
            name="代理按条测试",
            subtitle="按条",
            description="测试",
            price="0.40",
            agent_floor_price="0.30",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        agent = Agent.objects.create(
            phone="13800000001",
            nickname="代理A",
            code="AGENT001",
            status=Agent.STATUS_ACTIVE,
            payee_name="代理A",
        )
        agent.set_password("agent-pass")
        agent.save(update_fields=["password"])
        AgentPackagePrice.objects.create(agent=agent, package=line_package, sale_price="0.55")

        response = self.client.get(reverse("agent_home", args=[agent.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "¥0.55")
        self.assertNotContains(response, "QQ 联系方式")
        self.assertNotContains(response, "有任何问题，请联系")
        self.assertNotContains(response, "专属购买")
        self.assertNotContains(response, "专属售价")
        self.assertContains(response, 'href="/a/AGENT001/"', count=2)

    def test_agent_public_page_shows_agent_contact_fields(self):
        line_package = Package.objects.create(
            name="代理联系方式展示测试",
            subtitle="按条",
            description="测试",
            price="0.40",
            agent_floor_price="0.30",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        agent = Agent.objects.create(
            phone="13800000011",
            nickname="代理联系A",
            code="AGENT011",
            status=Agent.STATUS_ACTIVE,
            payee_name="代理联系A",
            contact_qq="12345678",
            contact_wechat="wx-agent-a",
        )
        agent.set_password("agent-pass")
        agent.save(update_fields=["password"])
        AgentPackagePrice.objects.create(agent=agent, package=line_package, sale_price="0.55")

        response = self.client.get(reverse("agent_home", args=[agent.code]))

        self.assertContains(response, "QQ：12345678")
        self.assertContains(response, "微信：wx-agent-a")
        self.assertNotContains(response, "电话：")

    def test_agent_package_detail_hides_exclusive_wording(self):
        line_package = Package.objects.create(
            name="代理详情隐藏测试",
            subtitle="按条",
            description="测试",
            price="0.40",
            agent_floor_price="0.30",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        agent = Agent.objects.create(
            phone="13800000013",
            nickname="代理详情A",
            code="AGENT013",
            status=Agent.STATUS_ACTIVE,
            payee_name="代理详情A",
        )
        agent.set_password("agent-pass")
        agent.save(update_fields=["password"])
        AgentPackagePrice.objects.create(agent=agent, package=line_package, sale_price="0.55")

        response = self.client.get(reverse("agent_package_detail", args=[agent.code, line_package.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "专属下单")
        self.assertNotContains(response, "返回专属页")
        self.assertNotContains(response, "专属购买页")
        self.assertContains(response, 'href="/a/AGENT013/"', count=2)

    def test_agent_create_order_records_price_snapshots(self):
        line_package = Package.objects.create(
            name="代理按条下单测试",
            subtitle="按条",
            description="测试",
            price="0.40",
            agent_floor_price="0.30",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        agent = Agent.objects.create(
            phone="13800000002",
            nickname="代理B",
            code="AGENT002",
            status=Agent.STATUS_ACTIVE,
            payee_name="代理B",
        )
        agent.set_password("agent-pass")
        agent.save(update_fields=["password"])
        AgentPackagePrice.objects.create(agent=agent, package=line_package, sale_price="0.45")
        StockItem.objects.create(package=line_package, content="line-stock-1")
        StockItem.objects.create(package=line_package, content="line-stock-2")

        response = self.client.post(
            reverse("agent_create_order", args=[agent.code, line_package.pk]),
            {
                "buyer_name": "测试买家",
                "buyer_contact": "wx-agent",
                "pickup_password": "123456",
                "quantity": "2",
            },
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(agent=agent)
        self.assertEqual(order.amount, Decimal("0.90"))
        self.assertEqual(order.agent_base_price_snapshot, Decimal("0.30"))
        self.assertEqual(order.agent_sale_price_snapshot, Decimal("0.45"))
        self.assertEqual(order.agent_profit_snapshot, Decimal("0.30"))
        self.assertEqual(order.agent_code_snapshot, agent.code)

    def test_agent_dashboard_shows_own_sales_summary(self):
        group_package = Package.objects.create(
            name="代理按组统计测试",
            subtitle="按组",
            description="测试",
            price="30.00",
            agent_floor_price="25.00",
            original_price="30.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_GROUP,
        )
        agent = Agent.objects.create(
            phone="13800000003",
            nickname="代理C",
            code="AGENT003",
            status=Agent.STATUS_ACTIVE,
            payee_name="代理C",
        )
        agent.set_password("agent-pass")
        agent.save(update_fields=["password"])
        order = Order.objects.create(
            order_no="ORDER-AGENT-DASH",
            package=group_package,
            buyer_contact="agent-dash",
            agent=agent,
            agent_code_snapshot=agent.code,
            agent_base_price_snapshot="25.00",
            agent_sale_price_snapshot="30.00",
            agent_profit_snapshot="5.00",
            agent_settlement_status=Order.AGENT_SETTLEMENT_PENDING,
            pickup_password=make_password("123456"),
            amount="30.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )
        session = self.client.session
        session[AGENT_SESSION_KEY] = agent.pk
        session.save()

        response = self.client.get(reverse("agent_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "总订单：1")
        self.assertContains(response, "待结算：¥5")
        self.assertContains(response, order.order_no)
        self.assertContains(response, "你自己加价卖出去的部分，全部算你的佣金。")
        self.assertContains(response, "平台设定的最低金额只是底价，本身不参与提成。")
        self.assertContains(response, "结算说明：代理佣金默认每周统一结算一次。")

    def test_agent_apply_page_and_success_message_explain_contact_step(self):
        response = self.client.get(reverse("agent_apply"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "系统会自动开通你的代理后台并生成专属推广链接")
        self.assertContains(response, "同一个 IP 只能注册 1 个代理账号")
        self.assertContains(response, "邮箱验证码")

        create_agent_email_code("agent88@example.com", AgentEmailVerification.PURPOSE_REGISTER)

        response = self.client.post(
            reverse("agent_apply"),
            {
                "phone": "13800000088",
                "email": "agent88@example.com",
                "email_code": "123456",
                "nickname": "代理联系提示",
                "password": "agent-pass",
                "contact_qq": "",
                "contact_wechat": "",
                "wechat_id": "settle-wx",
                "alipay_account": "",
                "payee_name": "联系人",
            },
            follow=True,
            REMOTE_ADDR="10.20.30.40",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "欢迎加入代理体系")
        self.assertContains(response, "专属推广链接")
        self.assertContains(response, "登录账号：13800000088")
        agent = Agent.objects.get(phone="13800000088")
        self.assertEqual(agent.status, Agent.STATUS_ACTIVE)
        self.assertEqual(agent.register_ip, "10.20.30.40")
        self.assertIsNotNone(agent.approved_at)
        self.assertEqual(agent.email, "agent88@example.com")
        self.assertTrue(agent.email_verified)

    def test_agent_apply_blocks_same_ip_registration(self):
        create_agent_email_code("agent111@example.com", AgentEmailVerification.PURPOSE_REGISTER)
        response = self.client.post(
            reverse("agent_apply"),
            {
                "phone": "13800000111",
                "email": "agent111@example.com",
                "email_code": "123456",
                "nickname": "代理一号",
                "password": "agent-pass",
                "contact_qq": "",
                "contact_wechat": "",
                "wechat_id": "settle-a",
                "alipay_account": "",
                "payee_name": "联系人A",
            },
            REMOTE_ADDR="66.77.88.99",
        )
        self.assertEqual(response.status_code, 302)

        create_agent_email_code("agent112@example.com", AgentEmailVerification.PURPOSE_REGISTER)
        response = self.client.post(
            reverse("agent_apply"),
            {
                "phone": "13800000112",
                "email": "agent112@example.com",
                "email_code": "123456",
                "nickname": "代理二号",
                "password": "agent-pass",
                "contact_qq": "",
                "contact_wechat": "",
                "wechat_id": "settle-b",
                "alipay_account": "",
                "payee_name": "联系人B",
            },
            follow=True,
            REMOTE_ADDR="66.77.88.99",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "当前 IP 已注册过代理账号")
        self.assertFalse(Agent.objects.filter(phone="13800000112").exists())

    def test_agent_can_update_public_contact_profile(self):
        agent = Agent.objects.create(
            phone="13800000012",
            nickname="代理资料A",
            code="AGENT012",
            status=Agent.STATUS_ACTIVE,
            payee_name="资料A",
        )
        agent.set_password("agent-pass")
        agent.save(update_fields=["password"])
        session = self.client.session
        session[AGENT_SESSION_KEY] = agent.pk
        session.save()

        response = self.client.post(
            reverse("agent_update_profile"),
            {
                "nickname": "代理资料B",
                "contact_qq": "99887766",
                "contact_wechat": "wx-agent-b",
                "wechat_id": "settle-wechat",
                "alipay_account": "settle@ali.com",
                "payee_name": "资料B",
            },
        )

        self.assertEqual(response.status_code, 302)
        agent.refresh_from_db()
        self.assertEqual(agent.nickname, "代理资料B")
        self.assertEqual(agent.contact_qq, "99887766")
        self.assertEqual(agent.contact_wechat, "wx-agent-b")
        self.assertEqual(agent.contact_phone, "")

    def test_agent_can_change_password_from_dashboard(self):
        agent = Agent.objects.create(
            phone="13800000018",
            nickname="代理改密A",
            code="AGENT018",
            status=Agent.STATUS_ACTIVE,
            payee_name="改密A",
        )
        agent.set_password("old-pass")
        agent.save(update_fields=["password"])
        session = self.client.session
        session[AGENT_SESSION_KEY] = agent.pk
        session.save()

        response = self.client.post(
            reverse("agent_update_password"),
            {
                "current_password": "old-pass",
                "new_password": "new-pass-123",
                "confirm_password": "new-pass-123",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "代理后台密码已修改成功")
        agent.refresh_from_db()
        self.assertTrue(agent.check_password("new-pass-123"))

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_agent_send_register_email_code(self):
        response = self.client.post(
            reverse("agent_send_email_code"),
            {"purpose": AgentEmailVerification.PURPOSE_REGISTER, "email": "register-code@example.com"},
            REMOTE_ADDR="1.2.3.4",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("验证码", mail.outbox[0].subject)
        self.assertTrue(
            AgentEmailVerification.objects.filter(
                email="register-code@example.com",
                purpose=AgentEmailVerification.PURPOSE_REGISTER,
            ).exists()
        )

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_agent_send_reset_email_code_does_not_enumerate_account(self):
        response = self.client.post(
            reverse("agent_send_email_code"),
            {"purpose": AgentEmailVerification.PURPOSE_RESET, "email": "missing-reset@example.com"},
            REMOTE_ADDR="2.3.4.5",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("如果 missing-reset@example.com 已绑定可用的代理账号", payload["message"])
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(APP_RATE_LIMIT_RULES=[])
    def test_agent_password_reset_locks_after_many_wrong_codes(self):
        agent = Agent.objects.create(
            phone="13800000031",
            nickname="代理锁定",
            code="AGENT031",
            status=Agent.STATUS_ACTIVE,
            payee_name="锁定",
            email="reset31@example.com",
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        agent.set_password("old-pass")
        agent.save(update_fields=["password"])
        create_agent_email_code("reset31@example.com", AgentEmailVerification.PURPOSE_RESET, code="654321")

        for _ in range(project_settings.AGENT_EMAIL_CODE_MAX_ATTEMPTS):
            response = self.client.post(
                reverse("agent_password_reset"),
                {
                    "email": "reset31@example.com",
                    "email_code": "000000",
                    "new_password": "reset-pass-123",
                    "confirm_password": "reset-pass-123",
                },
                REMOTE_ADDR="9.9.9.9",
                follow=True,
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse("agent_password_reset"),
            {
                "email": "reset31@example.com",
                "email_code": "654321",
                "new_password": "reset-pass-123",
                "confirm_password": "reset-pass-123",
            },
            REMOTE_ADDR="9.9.9.9",
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "邮箱验证码尝试次数过多")
        agent.refresh_from_db()
        self.assertTrue(agent.check_password("old-pass"))

    def test_agent_login_rate_limited(self):
        agent = Agent.objects.create(
            phone="13800000032",
            nickname="代理限流",
            code="AGENT032",
            status=Agent.STATUS_ACTIVE,
            payee_name="限流",
        )
        agent.set_password("agent-pass-123")
        agent.save(update_fields=["password"])

        last_response = None
        for _ in range(9):
            last_response = self.client.post(
                reverse("agent_login"),
                {"phone": "13800000032", "password": "bad-pass"},
                REMOTE_ADDR="7.7.7.7",
            )

        self.assertIsNotNone(last_response)
        self.assertEqual(last_response.status_code, 429)
        self.assertIn("操作太频繁了", last_response.content.decode("utf-8"))

    def test_agent_apply_does_not_trust_forged_forwarded_for(self):
        create_agent_email_code("agent113@example.com", AgentEmailVerification.PURPOSE_REGISTER)
        response = self.client.post(
            reverse("agent_apply"),
            {
                "phone": "13800000113",
                "email": "agent113@example.com",
                "email_code": "123456",
                "nickname": "代理防伪造",
                "password": "agent-pass-123",
                "contact_qq": "",
                "contact_wechat": "",
                "wechat_id": "settle-c",
                "alipay_account": "",
                "payee_name": "联系人C",
            },
            REMOTE_ADDR="55.66.77.88",
            HTTP_X_FORWARDED_FOR="1.1.1.1",
            HTTP_CF_CONNECTING_IP="2.2.2.2",
        )

        self.assertEqual(response.status_code, 302)
        agent = Agent.objects.get(phone="13800000113")
        self.assertEqual(agent.register_ip, "55.66.77.88")

    def test_agent_dashboard_reminds_existing_agent_to_bind_email(self):
        agent = Agent.objects.create(
            phone="13800000028",
            nickname="代理邮箱提醒",
            code="AGENT028",
            status=Agent.STATUS_ACTIVE,
            payee_name="邮箱提醒",
        )
        agent.set_password("agent-pass")
        agent.save(update_fields=["password"])
        session = self.client.session
        session[AGENT_SESSION_KEY] = agent.pk
        session.save()

        response = self.client.get(reverse("agent_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请先绑定邮箱")
        self.assertContains(response, "以后找回密码、修改密码和接收重要提醒")

    def test_agent_can_bind_email_from_dashboard(self):
        agent = Agent.objects.create(
            phone="13800000029",
            nickname="代理绑邮",
            code="AGENT029",
            status=Agent.STATUS_ACTIVE,
            payee_name="绑邮",
        )
        agent.set_password("agent-pass")
        agent.save(update_fields=["password"])
        create_agent_email_code("bind29@example.com", AgentEmailVerification.PURPOSE_BIND, agent=agent)
        session = self.client.session
        session[AGENT_SESSION_KEY] = agent.pk
        session.save()

        response = self.client.post(
            reverse("agent_bind_email"),
            {"email": "bind29@example.com", "email_code": "123456"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "绑定邮箱成功")
        agent.refresh_from_db()
        self.assertEqual(agent.email, "bind29@example.com")
        self.assertTrue(agent.email_verified)

    def test_agent_can_reset_password_by_email_code(self):
        agent = Agent.objects.create(
            phone="13800000030",
            nickname="代理找回",
            code="AGENT030",
            status=Agent.STATUS_ACTIVE,
            payee_name="找回",
            email="reset30@example.com",
            email_verified=True,
            email_verified_at=timezone.now(),
        )
        agent.set_password("old-pass")
        agent.save(update_fields=["password"])
        create_agent_email_code("reset30@example.com", AgentEmailVerification.PURPOSE_RESET)

        response = self.client.post(
            reverse("agent_password_reset"),
            {
                "email": "reset30@example.com",
                "email_code": "123456",
                "new_password": "reset-pass-123",
                "confirm_password": "reset-pass-123",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "密码已重置成功")
        agent.refresh_from_db()
        self.assertTrue(agent.check_password("reset-pass-123"))

    def test_agent_admin_can_reset_password_to_phone_suffix(self):
        agent = Agent.objects.create(
            phone="13800004567",
            nickname="代理重置A",
            code="AGENT019",
            status=Agent.STATUS_ACTIVE,
            payee_name="重置A",
        )
        agent.set_password("old-secret")
        agent.save(update_fields=["password"])

        model_admin = AgentAdmin(Agent, AdminSite())
        request = RequestFactory().post("/admin/store/agent/")
        request.user = get_user_model().objects.create_superuser(
            username="agentadminreset",
            email="agentadminreset@example.com",
            password="admin-pass-123",
        )
        setattr(request, "session", self.client.session)
        setattr(request, "_messages", FallbackStorage(request))
        model_admin.reset_password_to_phone_suffix(request, Agent.objects.filter(pk=agent.pk))

        agent.refresh_from_db()
        self.assertTrue(agent.check_password("004567"))

    def test_agent_can_upload_contact_images_and_public_page_shows_them(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                agent = Agent.objects.create(
                    phone="13800000015",
                    nickname="代理图片A",
                    code="AGENT015",
                    status=Agent.STATUS_ACTIVE,
                    payee_name="代理图片A",
                )
                agent.set_password("agent-pass")
                agent.save()
                session = self.client.session
                session[AGENT_SESSION_KEY] = agent.pk
                session.save()

                response = self.client.post(
                    reverse("agent_update_profile"),
                    {
                        "nickname": "代理图片A",
                        "contact_qq": "112233",
                        "contact_wechat": "wx-agent-img",
                        "wechat_id": "settle-wechat",
                        "alipay_account": "img@ali.com",
                        "payee_name": "代理图片A",
                        "contact_image_1": make_test_image(),
                    },
                )

                self.assertEqual(response.status_code, 302)
                agent.refresh_from_db()
                self.assertTrue(agent.contact_image_1.name)
                with Image.open(agent.contact_image_1.path) as saved:
                    self.assertLessEqual(saved.width, 900)
                    self.assertLessEqual(saved.height, 900)

                response = self.client.get(reverse("agent_home", args=[agent.code]))
                self.assertContains(response, agent.contact_image_1.url)

    def test_home_page_uses_uploaded_site_contact_images(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                site_contact = SiteContactConfig.objects.create(
                    title="官网联系方式",
                    contact_image_1=make_test_image("site-contact.png", size=(1600, 1600)),
                )

                with Image.open(site_contact.contact_image_1.path) as saved:
                    self.assertLessEqual(saved.width, 900)
                    self.assertLessEqual(saved.height, 900)

                response = self.client.get(reverse("home"))
                self.assertContains(response, site_contact.contact_image_1.url)


@override_settings(
    APP_RATE_LIMIT_RULES=[
        {
            "name": "admin-login",
            "pattern": r"^/admin/login/$",
            "methods": ["POST"],
            "limit": 1,
            "window": 60,
            "block_message": "1 分钟",
        },
        {
            "name": "pickup-lookup",
            "pattern": r"^/pickup/$",
            "methods": ["POST"],
            "limit": 1,
            "window": 60,
            "block_message": "1 分钟",
        },
        {
            "name": "pickup-order",
            "pattern": r"^/orders/\d+/pickup/$",
            "methods": ["POST"],
            "limit": 1,
            "window": 60,
            "block_message": "1 分钟",
        },
        {
            "name": "create-order",
            "pattern": r"^/packages/\d+/buy/$",
            "methods": ["POST"],
            "limit": 1,
            "window": 60,
            "block_message": "1 分钟",
        },
        {
            "name": "start-payment",
            "pattern": r"^/orders/\d+/pay/$",
            "methods": ["POST"],
            "limit": 1,
            "window": 60,
            "block_message": "1 分钟",
        },
    ]
)
class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_pickup_lookup_is_limited_after_repeated_posts(self):
        first = self.client.post(
            reverse("pickup_lookup"),
            {"buyer_contact": "wx-001", "pickup_password": "bad-pass"},
        )
        second = self.client.post(
            reverse("pickup_lookup"),
            {"buyer_contact": "wx-001", "pickup_password": "bad-pass"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("操作太频繁了", second.content.decode("utf-8"))

    def test_admin_login_is_limited_after_repeated_posts(self):
        user_model = get_user_model()
        user_model.objects.create_superuser("admin", "admin@example.com", "Admin123456!")

        first = self.client.post(
            "/admin/login/",
            {"username": "admin", "password": "wrong-pass"},
        )
        second = self.client.post(
            "/admin/login/",
            {"username": "admin", "password": "wrong-pass"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("操作太频繁了", second.content.decode("utf-8"))

    def test_create_order_is_limited_after_repeated_posts(self):
        package = Package.objects.create(
            name="限速下单商品",
            subtitle="按条",
            description="测试",
            price="0.40",
            original_price="0.40",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        StockItem.objects.create(package=package, content="x----1----a")
        StockItem.objects.create(package=package, content="y----2----b")

        first = self.client.post(
            reverse("create_order", args=[package.pk]),
            {"buyer_contact": "wx-001", "pickup_password": "abc123", "quantity": "1"},
        )
        second = self.client.post(
            reverse("create_order", args=[package.pk]),
            {"buyer_contact": "wx-001", "pickup_password": "abc123", "quantity": "1"},
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 429)
        self.assertIn("操作太频繁了", second.content.decode("utf-8"))

    def test_start_payment_is_limited_after_repeated_posts(self):
        order = Order.objects.create(
            order_no="ORDER-RATE-PAY",
            package=Package.objects.create(
                name="限速支付商品",
                subtitle="支付",
                description="测试",
                price="19.90",
                original_price="19.90",
            ),
            buyer_contact="wx-pay",
            pickup_password=make_password("abc123"),
            amount="19.90",
        )

        first = self.client.post(reverse("start_payment", args=[order.pk]))
        second = self.client.post(reverse("start_payment", args=[order.pk]))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("操作太频繁了", second.content.decode("utf-8"))


class SecurityConfigTests(TestCase):
    def test_settings_require_secret_key_when_debug_false(self):
        with patch.dict(
            project_settings.os.environ,
            {"DEBUG": "False"},
            clear=False,
        ):
            old_secret = project_settings.os.environ.pop("SECRET_KEY", None)
            with self.assertRaises(ImproperlyConfigured):
                reload(project_settings)
            project_settings.os.environ["SECRET_KEY"] = old_secret or "test-secret-key"
            reload(project_settings)

    def test_stock_item_admin_clear_selected_sold_stock_skips_unsold(self):
        package = Package.objects.create(
            name="库存列表清理商品",
            subtitle="按条",
            description="测试",
            price="1.00",
            original_price="1.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        order = Order.objects.create(
            order_no="ORDER-STOCK-ADMIN",
            package=package,
            buyer_contact="wx-stock-admin",
            pickup_password=make_password("123456"),
            amount="1.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )
        sold_item = StockItem.objects.create(
            package=package,
            content="sold-stock",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now(),
        )
        unsold_item = StockItem.objects.create(package=package, content="unsold-stock")

        request = RequestFactory().post("/admin/store/stockitem/")
        request.session = self.client.session
        setattr(request, "_messages", FallbackStorage(request))
        admin_obj = StockItemAdmin(StockItem, AdminSite())
        admin_obj.clear_selected_sold_stock(
            request,
            StockItem.objects.filter(pk__in=[sold_item.pk, unsold_item.pk]),
        )

        self.assertFalse(StockItem.objects.filter(pk=sold_item.pk).exists())
        self.assertTrue(StockItem.objects.filter(pk=unsold_item.pk).exists())

    def test_stock_item_admin_queryset_orders_sold_items_first(self):
        package = Package.objects.create(
            name="库存排序商品",
            subtitle="按条",
            description="测试",
            price="1.00",
            original_price="1.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        order = Order.objects.create(
            order_no="ORDER-STOCK-ORDERING",
            package=package,
            buyer_contact="wx-stock-ordering",
            pickup_password=make_password("123456"),
            amount="1.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )
        older_sold = StockItem.objects.create(
            package=package,
            content="older-sold",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now() - timedelta(minutes=5),
        )
        newer_sold = StockItem.objects.create(
            package=package,
            content="newer-sold",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now(),
        )
        unsold_item = StockItem.objects.create(package=package, content="unsold-item")

        request = RequestFactory().get("/admin/store/stockitem/")
        admin_obj = StockItemAdmin(StockItem, AdminSite())
        ids = list(admin_obj.get_queryset(request).values_list("id", flat=True)[:3])

        self.assertEqual(ids, [newer_sold.pk, older_sold.pk, unsold_item.pk])

    def test_order_admin_delivered_stock_block_shows_allocated_items(self):
        package = Package.objects.create(
            name="订单后台发货展示",
            subtitle="按条",
            description="测试",
            price="1.00",
            original_price="1.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        order = Order.objects.create(
            order_no="ORDER-ADMIN-STOCK",
            package=package,
            buyer_contact="wx-admin-stock",
            pickup_password=make_password("123456"),
            amount="1.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )
        item = StockItem.objects.create(
            package=package,
            content="mail@example.com----pass123",
            inbox_url="https://fbvjdjcf.asia",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now(),
        )

        admin_obj = OrderAdmin(Order, AdminSite())
        html = str(admin_obj.delivered_stock_block(order))

        self.assertIn("mail@example.com----pass123", html)
        self.assertIn("https://fbvjdjcf.asia", html)
        self.assertIn(str(item.pk), html)

    def test_stock_item_inline_orders_sold_items_first(self):
        package = Package.objects.create(
            name="内联库存排序商品",
            subtitle="按条",
            description="测试",
            price="1.00",
            original_price="1.00",
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=Package.STOCK_LINE,
        )
        order = Order.objects.create(
            order_no="ORDER-STOCK-INLINE",
            package=package,
            buyer_contact="wx-stock-inline",
            pickup_password=make_password("123456"),
            amount="1.00",
            status=Order.STATUS_PAID,
            paid_at=timezone.now(),
        )
        older_sold = StockItem.objects.create(
            package=package,
            content="inline-older-sold",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now() - timedelta(minutes=5),
        )
        newer_sold = StockItem.objects.create(
            package=package,
            content="inline-newer-sold",
            is_sold=True,
            sold_order=order,
            sold_at=timezone.now(),
        )
        unsold_item = StockItem.objects.create(package=package, content="inline-unsold")

        request = RequestFactory().get(f"/admin/store/package/{package.pk}/change/")
        request.user = get_user_model().objects.create_superuser(
            username="inlineadmin",
            email="inlineadmin@example.com",
            password="admin123456",
        )
        inline = StockItemInline(Package, AdminSite())
        ids = list(inline.get_queryset(request).filter(package=package).values_list("id", flat=True))

        self.assertEqual(ids[:3], [newer_sold.pk, older_sold.pk, unsold_item.pk])
