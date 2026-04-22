from decimal import Decimal
import hashlib
from io import BytesIO
from pathlib import Path

from django.contrib.auth.hashers import check_password, make_password
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.db import models
from PIL import Image, ImageOps


CONTACT_IMAGE_MAX_SIZE = (900, 900)
CONTACT_IMAGE_QUALITY = 82


def _compress_contact_image(file_obj, name_prefix):
    file_obj.seek(0)
    image = Image.open(file_obj)
    image = ImageOps.exif_transpose(image)
    has_alpha = "A" in image.getbands()
    target_mode = "RGBA" if has_alpha else "RGB"
    if image.mode != target_mode:
        image = image.convert(target_mode)
    image.thumbnail(CONTACT_IMAGE_MAX_SIZE, Image.Resampling.LANCZOS)

    output = BytesIO()
    if has_alpha:
        image.save(output, format="WEBP", quality=CONTACT_IMAGE_QUALITY, method=6)
        extension = "webp"
    else:
        image = image.convert("RGB")
        image.save(output, format="JPEG", quality=CONTACT_IMAGE_QUALITY, optimize=True)
        extension = "jpg"

    stem = Path(getattr(file_obj, "name", "")).stem or name_prefix
    safe_stem = "".join(ch for ch in stem if ch.isalnum() or ch in ("-", "_")).strip() or name_prefix
    return ContentFile(output.getvalue(), name=f"{safe_stem}.{extension}")


def build_contact_image_sha256(file_obj, name_prefix="contact_image"):
    compressed = _compress_contact_image(file_obj, name_prefix)
    compressed.open()
    payload = compressed.read()
    compressed.seek(0)
    return hashlib.sha256(payload).hexdigest()


def get_saved_image_sha256(file_field):
    if not file_field:
        return ""
    file_field.open("rb")
    try:
        return hashlib.sha256(file_field.read()).hexdigest()
    finally:
        file_field.close()


def _prepare_image_fields(instance, field_names, prefix):
    for field_name in field_names:
        file_field = getattr(instance, field_name)
        if not file_field or getattr(file_field, "_committed", True):
            continue
        setattr(instance, field_name, _compress_contact_image(file_field, f"{prefix}_{field_name}"))


class Document(models.Model):
    title = models.CharField(max_length=200, verbose_name="文档标题")
    summary = models.TextField(blank=True, verbose_name="文档简介")
    file = models.FileField(upload_to="documents/", blank=True, null=True, verbose_name="文档文件")
    cover = models.ImageField(upload_to="covers/", blank=True, null=True, verbose_name="封面图")
    is_active = models.BooleanField(default=True, verbose_name="是否上架")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "文档"
        verbose_name_plural = "文档"

    def __str__(self):
        return self.title


class Package(models.Model):
    DELIVERY_DOCS = "docs"
    DELIVERY_STOCK = "stock"
    STOCK_LINE = "line"
    STOCK_GROUP = "group"

    DELIVERY_CHOICES = [
        (DELIVERY_DOCS, "文档套餐"),
        (DELIVERY_STOCK, "库存发货"),
    ]
    STOCK_MODE_CHOICES = [
        (STOCK_LINE, "按条售卖"),
        (STOCK_GROUP, "按组售卖"),
    ]

    name = models.CharField(max_length=200, verbose_name="商品名称")
    subtitle = models.CharField(max_length=255, blank=True, verbose_name="副标题")
    description = models.TextField(verbose_name="商品介绍")
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="售价")
    agent_floor_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="代理底价",
    )
    original_price = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="划线价")
    items_per_unit = models.PositiveIntegerField(default=1, verbose_name="每份发货条数")
    stock_mode = models.CharField(
        max_length=20,
        choices=STOCK_MODE_CHOICES,
        default=STOCK_LINE,
        verbose_name="库存售卖模式",
    )
    delivery_mode = models.CharField(
        max_length=20,
        choices=DELIVERY_CHOICES,
        default=DELIVERY_DOCS,
        verbose_name="发货模式",
    )
    documents = models.ManyToManyField(Document, related_name="packages", blank=True, verbose_name="包含文档")
    is_active = models.BooleanField(default=True, verbose_name="是否上架")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        ordering = ["id"]
        verbose_name = "商品"
        verbose_name_plural = "商品"

    def __str__(self):
        return self.name

    @property
    def available_stock_count(self):
        if self.stock_mode == self.STOCK_LINE:
            # 按条售卖：计算当前按条库存 + 组库存中可转换的数量
            line_count = self.stock_items.filter(is_sold=False).count()
            # 计算组库存中可转换的数量（每组减去1个主账号）
            group_packages = Package.objects.filter(
                stock_mode=self.STOCK_GROUP,
                delivery_mode=self.DELIVERY_STOCK,
                is_active=True
            )
            group_count = 0
            for group_package in group_packages:
                # 计算每组中的账号数量（通过内容行数计算）
                for stock_item in group_package.stock_items.filter(is_sold=False):
                    # 计算组内账号数量（排除分组标记行）
                    lines = [line.strip() for line in stock_item.content.splitlines() if line.strip() and "----" in line]
                    if lines:
                        # 减去1个主账号
                        group_count += max(0, len(lines) - 1)
            return line_count + group_count
        else:
            # 按组售卖：只计算组库存
            return self.stock_items.filter(is_sold=False).count()

    @property
    def available_unit_count(self):
        if self.delivery_mode != self.DELIVERY_STOCK:
            return 0
        return self.available_stock_count


class Order(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PAID = "paid"
    STATUS_ISSUE = "issue"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_PENDING, "待支付"),
        (STATUS_PAID, "已支付"),
        (STATUS_ISSUE, "支付成功待处理"),
        (STATUS_CLOSED, "已关闭"),
    ]
    AGENT_SETTLEMENT_PENDING = "pending"
    AGENT_SETTLEMENT_SETTLED = "settled"
    AGENT_SETTLEMENT_CHOICES = [
        (AGENT_SETTLEMENT_PENDING, "待结算"),
        (AGENT_SETTLEMENT_SETTLED, "已结算"),
    ]
    # 2FA状态选项
    TWOFA_NO = "no_2fa"
    TWOFA_HAS = "has_2fa"
    TWOFA_HAS_YOUTUBE = "has_2fa_youtube"
    TWOFA_STATUS_CHOICES = [
        (TWOFA_NO, "未开2fa"),
        (TWOFA_HAS, "已开通2fa"),
        (TWOFA_HAS_YOUTUBE, "已开通2fa可登录油管"),
    ]

    order_no = models.CharField(max_length=32, unique=True, verbose_name="订单号")
    package = models.ForeignKey(Package, on_delete=models.PROTECT, verbose_name="购买套餐")
    buyer_name = models.CharField(max_length=100, blank=True, verbose_name="购买人")
    buyer_contact = models.CharField(max_length=120, blank=True, verbose_name="联系方式")
    agent = models.ForeignKey(
        "Agent",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="orders",
        verbose_name="关联代理",
    )
    agent_code_snapshot = models.CharField(max_length=32, blank=True, verbose_name="代理编号快照")
    agent_base_price_snapshot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="代理底价快照",
    )
    agent_sale_price_snapshot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="代理售价快照",
    )
    agent_profit_snapshot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="代理利润快照",
    )
    agent_settlement_status = models.CharField(
        max_length=20,
        choices=AGENT_SETTLEMENT_CHOICES,
        default=AGENT_SETTLEMENT_PENDING,
        verbose_name="代理结算状态",
    )
    agent_settled_at = models.DateTimeField(blank=True, null=True, verbose_name="代理结算时间")
    pickup_password = models.CharField(max_length=255, blank=True, verbose_name="提取密码")
    quantity = models.PositiveIntegerField(default=1, verbose_name="购买数量")
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="订单金额")
    payment_type = models.CharField(max_length=30, blank=True, verbose_name="支付方式")
    gateway_trade_no = models.CharField(max_length=64, blank=True, verbose_name="平台订单号")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, verbose_name="订单状态")
    twofa_status = models.CharField(
        max_length=20,
        choices=TWOFA_STATUS_CHOICES,
        blank=True,
        default="",
        verbose_name="2FA状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    paid_at = models.DateTimeField(blank=True, null=True, verbose_name="支付时间")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "订单"
        verbose_name_plural = "订单"

    def __str__(self):
        return self.order_no

    @property
    def delivery_count(self):
        return self.quantity


class StockItem(models.Model):
    # 2FA状态选项
    TWOFA_NO = "no_2fa"
    TWOFA_HAS = "has_2fa"
    TWOFA_HAS_YOUTUBE = "has_2fa_youtube"
    TWOFA_STATUS_CHOICES = [
        (TWOFA_NO, "未开2fa"),
        (TWOFA_HAS, "已开通2fa"),
        (TWOFA_HAS_YOUTUBE, "已开通2fa可登录油管"),
    ]
    
    package = models.ForeignKey(
        Package,
        on_delete=models.CASCADE,
        related_name="stock_items",
        verbose_name="所属商品",
    )
    content = models.TextField(verbose_name="发货内容")
    inbox_url = models.CharField(max_length=255, blank=True, default="", verbose_name="接码网址")
    twofa_status = models.CharField(
        max_length=20,
        choices=TWOFA_STATUS_CHOICES,
        default=TWOFA_NO,
        verbose_name="2FA状态",
    )
    is_sold = models.BooleanField(default=False, verbose_name="是否已售")
    sold_order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="stock_items",
        verbose_name="关联订单",
    )
    sold_at = models.DateTimeField(blank=True, null=True, verbose_name="售出时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        ordering = ["id"]
        verbose_name = "按条库存"
        verbose_name_plural = "按条库存"

    def __str__(self):
        return f"{self.package.name} #{self.pk}"


class Agent(models.Model):
    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_DISABLED = "disabled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "待审核"),
        (STATUS_ACTIVE, "已开通"),
        (STATUS_DISABLED, "已禁用"),
    ]

    phone = models.CharField(max_length=20, unique=True, verbose_name="手机号")
    password = models.CharField(max_length=255, verbose_name="登录密码")
    email = models.EmailField(blank=True, default="", verbose_name="绑定邮箱")
    email_verified = models.BooleanField(default=False, verbose_name="邮箱已验证")
    email_verified_at = models.DateTimeField(blank=True, null=True, verbose_name="邮箱验证时间")
    nickname = models.CharField(max_length=80, verbose_name="代理昵称")
    code = models.CharField(max_length=32, unique=True, verbose_name="代理编号")
    contact_qq = models.CharField(max_length=40, blank=True, verbose_name="公开QQ")
    contact_wechat = models.CharField(max_length=120, blank=True, verbose_name="公开微信号")
    contact_image_1 = models.ImageField(
        upload_to="contact_images/agents/",
        blank=True,
        null=True,
        verbose_name="联系方式图片1",
    )
    contact_image_2 = models.ImageField(
        upload_to="contact_images/agents/",
        blank=True,
        null=True,
        verbose_name="联系方式图片2",
    )
    contact_phone = models.CharField(max_length=20, blank=True, verbose_name="公开电话")
    wechat_id = models.CharField(max_length=120, blank=True, verbose_name="微信号")
    alipay_account = models.CharField(max_length=120, blank=True, verbose_name="支付宝账号")
    payee_name = models.CharField(max_length=80, blank=True, verbose_name="收款姓名")
    register_ip = models.CharField(max_length=64, blank=True, default="", verbose_name="注册IP")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        verbose_name="代理状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="开通时间")
    last_login_at = models.DateTimeField(blank=True, null=True, verbose_name="最近登录时间")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "代理"
        verbose_name_plural = "代理"

    def __str__(self):
        return f"{self.nickname} ({self.phone})"

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password)

    def save(self, *args, **kwargs):
        _prepare_image_fields(self, ("contact_image_1", "contact_image_2"), f"agent_{self.code or 'new'}")
        super().save(*args, **kwargs)

    @property
    def is_active_agent(self):
        return self.status == self.STATUS_ACTIVE


class AgentEmailVerification(models.Model):
    PURPOSE_REGISTER = "register"
    PURPOSE_BIND = "bind"
    PURPOSE_RESET = "reset"
    PURPOSE_CHOICES = [
        (PURPOSE_REGISTER, "代理注册"),
        (PURPOSE_BIND, "绑定邮箱"),
        (PURPOSE_RESET, "找回密码"),
    ]

    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="email_verifications",
        verbose_name="关联代理",
    )
    email = models.EmailField(verbose_name="邮箱")
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, verbose_name="用途")
    code = models.CharField(max_length=6, verbose_name="验证码")
    expires_at = models.DateTimeField(verbose_name="过期时间")
    used_at = models.DateTimeField(blank=True, null=True, verbose_name="使用时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "代理邮箱验证码"
        verbose_name_plural = "代理邮箱验证码"

    def __str__(self):
        return f"{self.email} - {self.get_purpose_display()}"


class SiteContactConfig(models.Model):
    title = models.CharField(max_length=80, default="官网联系方式", verbose_name="配置名称")
    contact_image_1 = models.ImageField(
        upload_to="contact_images/site/",
        blank=True,
        null=True,
        verbose_name="官网联系方式图片1",
    )
    contact_image_2 = models.ImageField(
        upload_to="contact_images/site/",
        blank=True,
        null=True,
        verbose_name="官网联系方式图片2",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["id"]
        verbose_name = "官网联系方式配置"
        verbose_name_plural = "官网联系方式配置"

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        _prepare_image_fields(self, ("contact_image_1", "contact_image_2"), "site_contact")
        super().save(*args, **kwargs)


class MailGatewaySyncConfig(models.Model):
    title = models.CharField(max_length=80, default="邮局白名单自动同步", verbose_name="配置名称")
    auto_sync_on_import = models.BooleanField(default=False, verbose_name="上传库存后自动同步白名单")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["id"]
        verbose_name = "邮局同步开关"
        verbose_name_plural = "邮局同步开关"

    def __str__(self):
        return self.title

    @classmethod
    def get_solo(cls):
        return cls.objects.order_by("id").first()


class AdminPasswordResetConfig(models.Model):
    title = models.CharField(max_length=80, default="后台找回密码配置", verbose_name="配置名称")
    reset_emails = models.TextField(blank=True, default="", verbose_name="管理员接收邮箱")
    code_expire_minutes = models.PositiveIntegerField(default=10, verbose_name="验证码有效期(分钟)")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["id"]
        verbose_name = "后台找回密码配置"
        verbose_name_plural = "后台找回密码配置"

    def __str__(self):
        return self.title

    @classmethod
    def get_solo(cls):
        return cls.objects.order_by("id").first()

    def parsed_reset_emails(self):
        values = []
        for raw in (self.reset_emails or "").replace("\r", "\n").split("\n"):
            for email in raw.split(","):
                cleaned = email.strip()
                if cleaned:
                    values.append(cleaned)
        return values


class AgentPackagePrice(models.Model):
    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        related_name="package_prices",
        verbose_name="代理",
    )
    package = models.ForeignKey(
        Package,
        on_delete=models.CASCADE,
        related_name="agent_prices",
        verbose_name="商品",
    )
    sale_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="代理售价")
    is_enabled = models.BooleanField(default=True, verbose_name="是否启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        ordering = ["package__id"]
        unique_together = ("agent", "package")
        verbose_name = "代理价格"
        verbose_name_plural = "代理价格"

    def __str__(self):
        return f"{self.agent.nickname} - {self.package.name}"

    def clean(self):
        if not self.package_id:
            return
        floor = self.package.agent_floor_price or self.package.price or Decimal("0")
        if self.sale_price < floor:
            raise ValidationError({"sale_price": f"代理售价不能低于底价 ¥{floor:.2f}"})
