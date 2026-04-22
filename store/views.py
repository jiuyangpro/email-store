import json
import random

from decimal import Decimal
from functools import wraps
from pathlib import Path
from datetime import timedelta
from time import time
from uuid import uuid4

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.conf import settings
from django.contrib import admin
from django.contrib import messages
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError

from .ckkp import sign_payload, verify_payload
from .models import (
    Agent,
    AgentEmailVerification,
    AdminPasswordResetConfig,
    AgentPackagePrice,
    Order,
    Package,
    SiteContactConfig,
    StockItem,
    build_contact_image_sha256,
    get_saved_image_sha256,
)


SEO_PAGES = {
    "google-mail": {
        "title": "企业邮箱购买_企业邮箱账号出售_企业邮箱商城",
        "description": "企业邮箱商城提供企业邮箱购买服务，支持按条购买和按组购买，库存实时同步，支付成功后自助提取。",
        "keywords": "企业邮箱,企业邮箱购买,企业邮箱账号,企业邮箱出售",
        "heading": "企业邮箱购买",
        "intro": "企业邮箱商城提供企业邮箱购买服务，支持按条购买和按组购买，库存实时同步，适合需要稳定补货和自助提取的场景。",
    },
    "enterprise-google-mail": {
        "title": "企业邮箱购买_企业邮箱账号_企业邮箱商城",
        "description": "企业邮箱商城提供企业邮箱账号，支持按条购买和按组购买，购买后可自助提取，按组购买享有管理员权限。",
        "keywords": "企业邮箱,企业邮箱购买,企业邮箱账号,企业邮箱出售",
        "heading": "企业邮箱购买",
        "intro": "如果你需要企业邮箱账号，本站支持企业邮箱按条购买和按组购买，按组购买享有管理员权限，适合整组交付。",
    },
    "google-mail-buy": {
        "title": "企业邮箱购买网站_企业邮箱购买_企业邮箱商城",
        "description": "企业邮箱商城是企业邮箱购买网站，支持企业邮箱按条购买、按组购买、库存实时同步和支付后自助提取。",
        "keywords": "企业邮箱购买网站,企业邮箱购买,企业邮箱出售,企业邮箱商城",
        "heading": "企业邮箱购买网站",
        "intro": "本站为企业邮箱购买网站，提供企业邮箱按条购买和按组购买能力，页面库存实时同步，支付成功后可直接回本站自助提取。",
    },
}

AGENT_SESSION_KEY = "agent_id"
MAIL_INBOX_URL = "https://shop.lncbeidfr.asia/"
MAIL_INBOX_PASSWORD = "123456"
AGENT_EMAIL_CODE_PURPOSES = {
    AgentEmailVerification.PURPOSE_REGISTER: "代理注册",
    AgentEmailVerification.PURPOSE_BIND: "绑定邮箱",
    AgentEmailVerification.PURPOSE_RESET: "找回密码",
}
ADMIN_PASSWORD_RESET_CACHE_PREFIX = "admin_password_reset_code"


def _admin_password_reset_cache_key(username: str) -> str:
    return f"{ADMIN_PASSWORD_RESET_CACHE_PREFIX}:{username.lower()}"


def _get_admin_reset_emails():
    config = AdminPasswordResetConfig.get_solo()
    if config:
        emails = config.parsed_reset_emails()
        if emails:
            return emails
    return [email.strip() for email in getattr(settings, "ADMIN_PASSWORD_RESET_EMAILS", []) if email.strip()]


def _get_admin_reset_expire_minutes():
    config = AdminPasswordResetConfig.get_solo()
    if config and int(config.code_expire_minutes or 0) > 0:
        return int(config.code_expire_minutes)
    return int(getattr(settings, "ADMIN_PASSWORD_RESET_CODE_EXPIRE_MINUTES", 10))


def _send_admin_password_reset_code(user, code):
    recipients = _get_admin_reset_emails()
    if not recipients:
        raise ValidationError("后台未配置管理员找回密码接收邮箱。")
    subject = "企业谷歌商城后台找回密码验证码"
    message = (
        f"你正在重置企业谷歌商城后台管理员账号 {user.username} 的密码。\n\n"
        f"验证码：{code}\n"
        f"有效期：{_get_admin_reset_expire_minutes()} 分钟\n\n"
        "如果不是你本人操作，请忽略这封邮件。"
    )
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)


def admin_password_reset(request):
    username_value = ""
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        username_value = (request.POST.get("username") or "").strip()
        user = None
        if username_value:
            user = get_user_model().objects.filter(
                username=username_value,
                is_superuser=True,
                is_active=True,
            ).first()
        if action == "send":
            if not user:
                messages.error(request, "这里只支持后台管理员账号找回密码。")
            else:
                code = f"{random.randint(0, 999999):06d}"
                cache.set(
                    _admin_password_reset_cache_key(user.username),
                    {"user_id": user.id, "code": code},
                    timeout=_get_admin_reset_expire_minutes() * 60,
                )
                try:
                    _send_admin_password_reset_code(user, code)
                except Exception as exc:
                    messages.error(request, f"验证码发送失败：{exc}")
                else:
                    messages.success(request, "验证码已发送到管理员绑定邮箱，请去邮箱查看。")
        elif action == "reset":
            code = (request.POST.get("code") or "").strip()
            new_password = request.POST.get("new_password") or ""
            confirm_password = request.POST.get("confirm_password") or ""
            if not user:
                messages.error(request, "这里只支持后台管理员账号找回密码。")
            elif not code.isdigit() or len(code) != 6:
                messages.error(request, "验证码格式不正确。")
            elif new_password != confirm_password:
                messages.error(request, "两次输入的新密码不一致。")
            else:
                cached = cache.get(_admin_password_reset_cache_key(user.username))
                if not cached or int(cached.get("user_id", 0)) != user.id:
                    messages.error(request, "验证码不存在或已过期，请重新发送。")
                elif str(cached.get("code", "")) != code:
                    messages.error(request, "验证码不正确。")
                else:
                    try:
                        validate_password(new_password, user=user)
                    except ValidationError as exc:
                        messages.error(request, "；".join(exc.messages))
                    else:
                        user.set_password(new_password)
                        user.save(update_fields=["password"])
                        cache.delete(_admin_password_reset_cache_key(user.username))
                        messages.success(request, "后台管理员密码已重置成功，请用新密码登录。")
                        return redirect(reverse("admin:login"))
    context = {
        "title": "后台忘记密码",
        "site_header": admin.site.site_header,
        "site_title": admin.site.site_title,
        "username_value": username_value,
        "reset_emails": _get_admin_reset_emails(),
    }
    return render(request, "admin/password_reset_form.html", context)


def _get_client_ip(request):
    remote_addr = request.META.get("REMOTE_ADDR", "unknown").strip() or "unknown"
    if remote_addr not in getattr(settings, "TRUSTED_PROXY_IPS", set()):
        return remote_addr
    cf_ip = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()
    if cf_ip:
        return cf_ip
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return remote_addr


def robots_txt(request):
    site_base = _site_base_url(request)
    content = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /orders/",
            "Disallow: /payments/",
            f"Sitemap: {site_base}/sitemap.xml",
            "",
        ]
    )
    return HttpResponse(content, content_type="text/plain; charset=utf-8")


def sitemap_xml(request):
    site_base = _site_base_url(request)
    package_urls = [
        f"""  <url><loc>{site_base}/packages/{package.pk}/</loc><changefreq>hourly</changefreq><priority>0.8</priority></url>"""
        for package in Package.objects.filter(is_active=True, delivery_mode=Package.DELIVERY_STOCK).order_by("id")
    ]
    seo_urls = [
        f"""  <url><loc>{site_base}/{slug}/</loc><changefreq>daily</changefreq><priority>0.7</priority></url>"""
        for slug in SEO_PAGES
    ]
    urls = [
        f"""<?xml version="1.0" encoding="UTF-8"?>""",
        """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">""",
        f"""  <url><loc>{site_base}/</loc><changefreq>hourly</changefreq><priority>1.0</priority></url>""",
        *package_urls,
        *seo_urls,
        """</urlset>""",
    ]
    return HttpResponse("\n".join(urls), content_type="application/xml; charset=utf-8")


def seo_page(request, slug):
    page = SEO_PAGES.get(slug)
    if not page:
        return redirect("home")
    packages = Package.objects.filter(is_active=True)
    line_package = packages.filter(
        delivery_mode=Package.DELIVERY_STOCK,
        stock_mode=Package.STOCK_LINE,
    ).annotate(
        unsold_count=Count("stock_items", filter=Q(stock_items__is_sold=False))
    ).order_by("-unsold_count", "-id").first()
    group_package = packages.filter(
        delivery_mode=Package.DELIVERY_STOCK,
        stock_mode=Package.STOCK_GROUP,
    ).annotate(
        unsold_count=Count("stock_items", filter=Q(stock_items__is_sold=False))
    ).order_by("-unsold_count", "-id").first()
    return render(
        request,
        "store/seo_page.html",
        {
            "page": page,
            "slug": slug,
            "line_package": line_package,
            "group_package": group_package,
        },
    )


def agent_login_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        agent = _get_current_agent(request)
        if not agent:
            messages.error(request, "请先登录代理后台。")
            return redirect("agent_login")
        request.agent = agent
        return view_func(request, *args, **kwargs)

    return wrapped


def _get_current_agent(request):
    agent_id = request.session.get(AGENT_SESSION_KEY)
    if not agent_id:
        return None
    return Agent.objects.filter(pk=agent_id, status=Agent.STATUS_ACTIVE).first()


def _get_active_agent_by_code(code):
    return get_object_or_404(Agent, code=code, status=Agent.STATUS_ACTIVE)


def _generate_agent_code():
    return uuid4().hex[:10].upper()


def _normalize_email(email):
    return (email or "").strip().lower()


def _validate_agent_email(email):
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


def _build_email_code():
    return f"{random.randint(0, 999999):06d}"


def _agent_email_code_cache_key(email, purpose, ip):
    return f"agent-email-code:{purpose}:{email}:{ip}"


def _throttle_email_code(email, purpose, ip):
    cache_key = _agent_email_code_cache_key(email, purpose, ip)
    if cache.get(cache_key):
        return False
    cache.set(cache_key, 1, timeout=60)
    return True


def _agent_email_attempts_cache_key(email, purpose, ip):
    return f"agent-email-attempts:{purpose}:{_normalize_email(email)}:{ip}"


def _is_agent_email_code_locked(email, purpose, ip):
    return cache.get(_agent_email_attempts_cache_key(email, purpose, ip), 0) >= settings.AGENT_EMAIL_CODE_MAX_ATTEMPTS


def _register_agent_email_code_failure(email, purpose, ip):
    cache_key = _agent_email_attempts_cache_key(email, purpose, ip)
    attempts = cache.get(cache_key, 0) + 1
    cache.set(cache_key, attempts, timeout=settings.AGENT_EMAIL_CODE_EXPIRE_MINUTES * 60)
    return attempts


def _clear_agent_email_code_failures(email, purpose, ip):
    cache.delete(_agent_email_attempts_cache_key(email, purpose, ip))


def _validate_agent_password(password, *, user=None):
    password = (password or "").strip()
    if len(password) < settings.AGENT_LOGIN_PASSWORD_MIN_LENGTH:
        raise ValidationError(f"密码长度不能少于 {settings.AGENT_LOGIN_PASSWORD_MIN_LENGTH} 位。")
    validate_password(password, user=user)
    return password


def _create_agent_email_verification(email, purpose, agent=None):
    AgentEmailVerification.objects.filter(
        email=email,
        purpose=purpose,
        agent=agent,
        used_at__isnull=True,
    ).delete()
    return AgentEmailVerification.objects.create(
        agent=agent,
        email=email,
        purpose=purpose,
        code=_build_email_code(),
        expires_at=timezone.now() + timedelta(minutes=settings.AGENT_EMAIL_CODE_EXPIRE_MINUTES),
    )


def _consume_agent_email_code(email, purpose, code, agent=None, *, client_ip=""):
    normalized_email = _normalize_email(email)
    if client_ip and _is_agent_email_code_locked(normalized_email, purpose, client_ip):
        return None, "locked"
    verification = (
        AgentEmailVerification.objects.filter(
            email=normalized_email,
            purpose=purpose,
            agent=agent,
            code=(code or "").strip(),
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )
    if not verification:
        if client_ip:
            _register_agent_email_code_failure(normalized_email, purpose, client_ip)
        return None, "invalid"
    verification.used_at = timezone.now()
    verification.save(update_fields=["used_at"])
    if client_ip:
        _clear_agent_email_code_failures(normalized_email, purpose, client_ip)
    return verification, "ok"


def _send_agent_email_code_message(email, purpose, code):
    purpose_label = AGENT_EMAIL_CODE_PURPOSES.get(purpose, "邮箱验证")
    subject = f"【企业邮箱商城】{purpose_label}验证码"
    message = (
        f"你的{purpose_label}验证码是：{code}\n\n"
        f"验证码 {settings.AGENT_EMAIL_CODE_EXPIRE_MINUTES} 分钟内有效。\n"
        "如果不是你本人操作，请忽略这封邮件。"
    )
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)


def _get_site_contact_config():
    return (
        SiteContactConfig.objects.filter(
            Q(contact_image_1__gt="") | Q(contact_image_2__gt="")
        ).order_by("-id").first()
        or SiteContactConfig.objects.order_by("-id").first()
    )


def _get_existing_contact_hashes(instance):
    hashes = []
    for field_name in ("contact_image_1", "contact_image_2"):
        image_hash = get_saved_image_sha256(getattr(instance, field_name, None))
        if image_hash:
            hashes.append(image_hash)
    return hashes


def _ensure_agent_package_price(agent, package):
    default_sale_price = package.price
    if package.agent_floor_price and package.agent_floor_price > 0:
        default_sale_price = package.agent_floor_price
    price_config, _ = AgentPackagePrice.objects.get_or_create(
        agent=agent,
        package=package,
        defaults={"sale_price": default_sale_price},
    )
    return price_config


def _ensure_agent_price_configs(agent):
    packages = list(
        Package.objects.filter(is_active=True, delivery_mode=Package.DELIVERY_STOCK).order_by("id")
    )
    price_configs = []
    for package in packages:
        price_configs.append(_ensure_agent_package_price(agent, package))
    return price_configs


def _get_agent_package_context(agent, stock_mode):
    package = (
        Package.objects.filter(
            is_active=True,
            delivery_mode=Package.DELIVERY_STOCK,
            stock_mode=stock_mode,
        )
        .annotate(unsold_count=Count("stock_items", filter=Q(stock_items__is_sold=False)))
        .order_by("-unsold_count", "-id")
        .first()
    )
    if not package:
        return None, None
    return package, _ensure_agent_package_price(agent, package)


def _create_order_from_request(request, package, sale_price, agent=None, detail_redirect=None):
    def _redirect_back():
        if detail_redirect:
            view_name, kwargs = detail_redirect
            return redirect(view_name, **kwargs)
        return redirect("package_detail", pk=package.pk)

    buyer_name = request.POST.get("buyer_name", "").strip()
    buyer_contact = request.POST.get("buyer_contact", "").strip()
    pickup_password = request.POST.get("pickup_password", "").strip()
    quantity = _parse_quantity(request.POST.get("quantity", "1"))
    twofa_status = request.POST.get("twofa_status", "")

    if not buyer_contact:
        messages.error(request, "请先填写联系方式。")
        return _redirect_back()
    if not pickup_password:
        messages.error(request, "请先设置提取密码。")
        return _redirect_back()
    if quantity < 1:
        messages.error(request, "购买数量至少为 1。")
        return _redirect_back()
    if package.delivery_mode == Package.DELIVERY_STOCK and quantity > package.available_unit_count:
        messages.error(request, "库存不足，当前没有这么多可发内容。")
        return _redirect_back()

    agent_base_price = Decimal("0.00")
    agent_sale_price = Decimal("0.00")
    agent_profit = Decimal("0.00")
    if agent:
        agent_base_price = package.agent_floor_price or package.price
        agent_sale_price = sale_price
        agent_profit = (agent_sale_price - agent_base_price) * quantity

    order = Order.objects.create(
        order_no=_generate_order_no(),
        package=package,
        buyer_name=buyer_name,
        buyer_contact=buyer_contact,
        agent=agent,
        agent_code_snapshot=agent.code if agent else "",
        agent_base_price_snapshot=agent_base_price,
        agent_sale_price_snapshot=agent_sale_price,
        agent_profit_snapshot=agent_profit,
        pickup_password=make_password(pickup_password),
        quantity=quantity,
        amount=sale_price * quantity,
    )
    
    # 保存 2FA 状态到订单对象
    if twofa_status:
        order.twofa_status = twofa_status
        order.save(update_fields=["twofa_status"])
    return redirect("order_detail", pk=order.pk)


def agent_apply(request):
    if request.method == "POST":
        client_ip = _get_client_ip(request)
        phone = request.POST.get("phone", "").strip()
        email = _normalize_email(request.POST.get("email", ""))
        email_code = request.POST.get("email_code", "").strip()
        nickname = request.POST.get("nickname", "").strip()
        password = request.POST.get("password", "").strip()
        contact_qq = request.POST.get("contact_qq", "").strip()
        contact_wechat = request.POST.get("contact_wechat", "").strip()
        wechat_id = request.POST.get("wechat_id", "").strip()
        alipay_account = request.POST.get("alipay_account", "").strip()
        payee_name = request.POST.get("payee_name", "").strip()

        if not phone or not nickname or not password or not email:
            messages.error(request, "请填写手机号、邮箱、昵称和登录密码。")
        elif not _validate_agent_email(email):
            messages.error(request, "请输入正确的邮箱地址。")
        elif not email_code:
            messages.error(request, "请先完成邮箱验证码验证。")
        elif not payee_name:
            messages.error(request, "请填写收款姓名，方便后续结算。")
        elif not (wechat_id or alipay_account):
            messages.error(request, "请至少填写一个微信号或支付宝账号。")
        elif Agent.objects.filter(phone=phone).exists():
            messages.error(request, "这个手机号已经提交过代理申请。")
        elif Agent.objects.filter(email__iexact=email, email_verified=True).exists():
            messages.error(request, "这个邮箱已经绑定过其他代理账号。")
        elif Agent.objects.filter(register_ip=client_ip).exists():
            messages.error(request, "当前 IP 已注册过代理账号，不能重复注册。")
        else:
            try:
                password = _validate_agent_password(password)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
                return render(request, "store/agent_apply.html")
            verification, verify_status = _consume_agent_email_code(
                email,
                AgentEmailVerification.PURPOSE_REGISTER,
                email_code,
                client_ip=client_ip,
            )
            if verify_status == "locked":
                messages.error(request, "邮箱验证码尝试次数过多，请稍后重新获取。")
                return render(request, "store/agent_apply.html")
            if not verification:
                messages.error(request, "邮箱验证码不对、已过期，或尝试次数过多，请重新获取。")
                return render(request, "store/agent_apply.html")
            agent = Agent(
                phone=phone,
                email=email,
                email_verified=True,
                email_verified_at=timezone.now(),
                nickname=nickname,
                code=_generate_agent_code(),
                contact_qq=contact_qq,
                contact_wechat=contact_wechat,
                wechat_id=wechat_id,
                alipay_account=alipay_account,
                payee_name=payee_name,
                register_ip=client_ip,
                status=Agent.STATUS_ACTIVE,
                approved_at=timezone.now(),
            )
            agent.set_password(password)
            agent.save()
            request.session[AGENT_SESSION_KEY] = agent.pk
            agent.last_login_at = timezone.now()
            agent.save(update_fields=["last_login_at"])
            messages.success(
                request,
                f"欢迎加入代理体系，{agent.nickname}。你的邮箱 {agent.email} 已绑定成功，专属推广链接已经生成，现在可以直接登录后台、改价格并开始推广。",
            )
            return redirect("agent_dashboard")

    return render(request, "store/agent_apply.html")


def agent_login(request):
    if request.method == "POST":
        phone = request.POST.get("phone", "").strip()
        password = request.POST.get("password", "").strip()
        agent = Agent.objects.filter(phone=phone).first()

        if not agent or not agent.check_password(password):
            messages.error(request, "手机号或密码不对。")
        elif agent.status != Agent.STATUS_ACTIVE:
            messages.error(request, "代理账号当前不可用，请联系管理员处理。")
        else:
            request.session[AGENT_SESSION_KEY] = agent.pk
            agent.last_login_at = timezone.now()
            agent.save(update_fields=["last_login_at"])
            messages.success(request, "代理后台登录成功。")
            return redirect("agent_dashboard")

    return render(request, "store/agent_login.html")


def agent_send_email_code(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "只支持 POST 请求。"}, status=405)

    purpose = request.POST.get("purpose", "").strip()
    email = _normalize_email(request.POST.get("email", ""))
    client_ip = _get_client_ip(request)
    agent = _get_current_agent(request) if purpose == AgentEmailVerification.PURPOSE_BIND else None

    if purpose not in AGENT_EMAIL_CODE_PURPOSES:
        return JsonResponse({"ok": False, "message": "验证码用途不正确。"}, status=400)
    if not email or not _validate_agent_email(email):
        return JsonResponse({"ok": False, "message": "请输入正确的邮箱地址。"}, status=400)
    if not _throttle_email_code(email, purpose, client_ip):
        return JsonResponse({"ok": False, "message": "发送太频繁了，请 1 分钟后再试。"}, status=429)
    if _is_agent_email_code_locked(email, purpose, client_ip):
        return JsonResponse({"ok": False, "message": "尝试次数过多，请稍后重新获取验证码。"}, status=429)

    if purpose == AgentEmailVerification.PURPOSE_BIND and not agent:
        return JsonResponse({"ok": False, "message": "请先登录代理后台。"}, status=403)

    if purpose in (AgentEmailVerification.PURPOSE_REGISTER, AgentEmailVerification.PURPOSE_BIND):
        queryset = Agent.objects.filter(email__iexact=email, email_verified=True)
        if agent:
            queryset = queryset.exclude(pk=agent.pk)
        if queryset.exists():
            return JsonResponse({"ok": False, "message": "这个邮箱已经绑定过其他代理账号。"}, status=400)

    if purpose == AgentEmailVerification.PURPOSE_RESET and not Agent.objects.filter(
        email__iexact=email, email_verified=True, status=Agent.STATUS_ACTIVE
    ).exists():
        return JsonResponse(
            {
                "ok": True,
                "message": f"如果 {email} 已绑定可用的代理账号，验证码会发送到该邮箱。请注意查收收件箱和垃圾邮箱。",
            }
        )

    verification = _create_agent_email_verification(email, purpose, agent=agent)
    try:
        _send_agent_email_code_message(email, purpose, verification.code)
    except Exception:
        verification.delete()
        return JsonResponse({"ok": False, "message": "验证码邮件发送失败，请稍后再试。"}, status=500)

    return JsonResponse(
        {
            "ok": True,
            "message": f"验证码已发送到 {email}，请注意查收收件箱和垃圾邮箱。",
        }
    )


def agent_logout(request):
    request.session.pop(AGENT_SESSION_KEY, None)
    messages.success(request, "已退出代理后台。")
    return redirect("agent_login")


def agent_public_home(request, code):
    agent = _get_active_agent_by_code(code)
    line_package, line_price = _get_agent_package_context(agent, Package.STOCK_LINE)
    group_package, group_price = _get_agent_package_context(agent, Package.STOCK_GROUP)
    site_home_url = reverse("agent_home", kwargs={"code": agent.code})
    return render(
        request,
        "store/agent_home.html",
        {
            "agent": agent,
            "site_home_url": site_home_url,
            "line_package": line_package,
            "line_price": line_price,
            "group_package": group_package,
            "group_price": group_price,
        },
    )


def agent_package_detail(request, code, pk):
    agent = _get_active_agent_by_code(code)
    package = get_object_or_404(
        Package.objects.filter(is_active=True).prefetch_related("documents"),
        pk=pk,
    )
    price_config = _ensure_agent_package_price(agent, package)
    quantity_options = range(1, package.available_unit_count + 1)
    site_home_url = reverse("agent_home", kwargs={"code": agent.code})
    return render(
        request,
        "store/agent_package_detail.html",
        {
            "agent": agent,
            "package": package,
            "agent_price": price_config,
            "site_home_url": site_home_url,
            "quantity_options": quantity_options,
            "unit_label": _unit_label(package),
            "stock_label": _stock_label(package),
        },
    )


def agent_create_order(request, code, pk):
    agent = _get_active_agent_by_code(code)
    if request.method != "POST":
        return redirect("agent_package_detail", code=agent.code, pk=pk)

    package = get_object_or_404(Package, pk=pk, is_active=True)
    price_config = _ensure_agent_package_price(agent, package)
    return _create_order_from_request(
        request=request,
        package=package,
        sale_price=price_config.sale_price,
        agent=agent,
        detail_redirect=("agent_package_detail", {"code": agent.code, "pk": pk}),
    )


@agent_login_required
def agent_dashboard(request):
    agent = request.agent
    price_configs = _ensure_agent_price_configs(agent)
    paid_statuses = [Order.STATUS_PAID, Order.STATUS_ISSUE]
    order_queryset = agent.orders.select_related("package").order_by("-created_at")
    sales_queryset = order_queryset.filter(status__in=paid_statuses)
    summary = sales_queryset.aggregate(
        total_orders=Count("id"),
        total_sales_amount=Sum("amount"),
        total_profit=Sum("agent_profit_snapshot"),
        unsettled_profit=Sum(
            "agent_profit_snapshot",
            filter=Q(agent_settlement_status=Order.AGENT_SETTLEMENT_PENDING),
        ),
    )
    context = {
        "agent": agent,
        "price_configs": price_configs,
        "recent_orders": sales_queryset[:20],
        "agent_public_url": f"{settings.SITE_BASE_URL}{reverse('agent_home', kwargs={'code': agent.code})}",
        "agent_login_url": f"{settings.SITE_BASE_URL}{reverse('agent_login')}",
        "summary": {
            "total_orders": summary["total_orders"] or 0,
            "total_sales_amount": summary["total_sales_amount"] or Decimal("0.00"),
            "total_profit": summary["total_profit"] or Decimal("0.00"),
            "unsettled_profit": summary["unsettled_profit"] or Decimal("0.00"),
            "settled_profit": (summary["total_profit"] or Decimal("0.00")) - (summary["unsettled_profit"] or Decimal("0.00")),
        },
        "email_binding_required": not (agent.email and agent.email_verified),
        "agent_contact_image_hashes_json": json.dumps(_get_existing_contact_hashes(agent)),
    }
    return render(request, "store/agent_dashboard.html", context)


@agent_login_required
def agent_bind_email(request):
    agent = request.agent
    if request.method != "POST":
        return redirect("agent_dashboard")

    email = _normalize_email(request.POST.get("email", ""))
    email_code = request.POST.get("email_code", "").strip()

    if not email or not _validate_agent_email(email):
        messages.error(request, "请输入正确的绑定邮箱。")
    elif Agent.objects.filter(email__iexact=email, email_verified=True).exclude(pk=agent.pk).exists():
        messages.error(request, "这个邮箱已经绑定过其他代理账号。")
    elif not email_code:
        messages.error(request, "请填写邮箱验证码。")
    else:
        verification, verify_status = _consume_agent_email_code(
            email,
            AgentEmailVerification.PURPOSE_BIND,
            email_code,
            agent=agent,
            client_ip=_get_client_ip(request),
        )
        if verify_status == "locked":
            messages.error(request, "邮箱验证码尝试次数过多，请稍后重新获取。")
            return redirect("agent_dashboard")
        if not verification:
            messages.error(request, "邮箱验证码不对、已过期，或尝试次数过多，请重新获取。")
            return redirect("agent_dashboard")
        agent.email = email
        agent.email_verified = True
        agent.email_verified_at = timezone.now()
        agent.save(update_fields=["email", "email_verified", "email_verified_at"])
        messages.success(request, f"绑定邮箱成功：{agent.email}。以后可通过邮箱找回密码。")

    return redirect("agent_dashboard")


@agent_login_required
def agent_update_profile(request):
    agent = request.agent
    if request.method != "POST":
        return redirect("agent_dashboard")

    agent.nickname = request.POST.get("nickname", "").strip() or agent.nickname
    agent.contact_qq = request.POST.get("contact_qq", "").strip()
    agent.contact_wechat = request.POST.get("contact_wechat", "").strip()
    agent.contact_phone = ""
    agent.wechat_id = request.POST.get("wechat_id", "").strip()
    agent.alipay_account = request.POST.get("alipay_account", "").strip()
    agent.payee_name = request.POST.get("payee_name", "").strip()
    existing_hashes = set(_get_existing_contact_hashes(agent))
    if request.POST.get("clear_contact_image_1"):
        agent.contact_image_1 = None
    elif request.FILES.get("contact_image_1"):
        image_1 = request.FILES["contact_image_1"]
        if (
            build_contact_image_sha256(image_1, "agent_contact_image_1") in existing_hashes
            and request.POST.get("allow_duplicate_contact_image_1") != "1"
        ):
            messages.error(request, "图片 1 已上传过。如确认要继续覆盖，请再次选择并确认继续上传。")
            return redirect("agent_dashboard")
        agent.contact_image_1 = image_1

    if request.POST.get("clear_contact_image_2"):
        agent.contact_image_2 = None
    elif request.FILES.get("contact_image_2"):
        image_2 = request.FILES["contact_image_2"]
        if (
            build_contact_image_sha256(image_2, "agent_contact_image_2") in existing_hashes
            and request.POST.get("allow_duplicate_contact_image_2") != "1"
        ):
            messages.error(request, "图片 2 已上传过。如确认要继续覆盖，请再次选择并确认继续上传。")
            return redirect("agent_dashboard")
        agent.contact_image_2 = image_2

    agent.save()
    messages.success(request, "联系方式和结算信息已保存。")
    return redirect("agent_dashboard")


@agent_login_required
def agent_update_password(request):
    agent = request.agent
    if request.method != "POST":
        return redirect("agent_dashboard")

    current_password = request.POST.get("current_password", "").strip()
    new_password = request.POST.get("new_password", "").strip()
    confirm_password = request.POST.get("confirm_password", "").strip()

    if not current_password or not new_password or not confirm_password:
        messages.error(request, "请填写当前密码、新密码和确认密码。")
    elif not agent.check_password(current_password):
        messages.error(request, "当前密码不对。")
    elif new_password != confirm_password:
        messages.error(request, "两次输入的新密码不一致。")
    else:
        try:
            new_password = _validate_agent_password(new_password, user=agent)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("agent_dashboard")
        agent.set_password(new_password)
        agent.save(update_fields=["password"])
        messages.success(request, "代理后台密码已修改成功，请用新密码登录。")

    return redirect("agent_dashboard")


def agent_password_reset(request):
    if request.method == "POST":
        email = _normalize_email(request.POST.get("email", ""))
        email_code = request.POST.get("email_code", "").strip()
        new_password = request.POST.get("new_password", "").strip()
        confirm_password = request.POST.get("confirm_password", "").strip()
        agent = Agent.objects.filter(email__iexact=email, email_verified=True, status=Agent.STATUS_ACTIVE).first()
        client_ip = _get_client_ip(request)

        if not email or not _validate_agent_email(email):
            messages.error(request, "请输入正确的绑定邮箱。")
        elif not email_code:
            messages.error(request, "请填写邮箱验证码。")
        elif new_password != confirm_password:
            messages.error(request, "两次输入的新密码不一致。")
        else:
            try:
                new_password = _validate_agent_password(new_password, user=agent)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
                return render(request, "store/agent_password_reset.html")
            verification, verify_status = _consume_agent_email_code(
                email,
                AgentEmailVerification.PURPOSE_RESET,
                email_code,
                client_ip=client_ip,
            )
            if verify_status == "locked":
                messages.error(request, "邮箱验证码尝试次数过多，请稍后重新获取。")
                return render(request, "store/agent_password_reset.html")
            if not verification or not agent:
                messages.error(request, "邮箱或验证码不正确，或尝试次数过多，请重新获取验证码。")
                return render(request, "store/agent_password_reset.html")
            agent.set_password(new_password)
            agent.save(update_fields=["password"])
            messages.success(request, "密码已重置成功，请使用新密码登录代理后台。")
            return redirect("agent_login")

    return render(request, "store/agent_password_reset.html")


@agent_login_required
def agent_update_prices(request):
    agent = request.agent
    if request.method != "POST":
        return redirect("agent_dashboard")

    price_configs = _ensure_agent_price_configs(agent)
    updated_count = 0
    for price_config in price_configs:
        field_name = f"sale_price_{price_config.package_id}"
        raw_value = request.POST.get(field_name, "").strip()
        if not raw_value:
            continue
        try:
            sale_price = Decimal(raw_value)
        except Exception:
            messages.error(request, f"{price_config.package.name} 的价格格式不对。")
            return redirect("agent_dashboard")

        floor_price = price_config.package.agent_floor_price or price_config.package.price
        if sale_price < floor_price:
            messages.error(
                request,
                f"{price_config.package.name} 的代理售价不能低于底价 ¥{floor_price:.2f}。",
            )
            return redirect("agent_dashboard")

        if sale_price != price_config.sale_price:
            price_config.sale_price = sale_price
            price_config.save(update_fields=["sale_price", "updated_at"])
            updated_count += 1

    if updated_count:
        messages.success(request, f"已保存 {updated_count} 个代理售价。")
    else:
        messages.success(request, "价格没有变化。")
    return redirect("agent_dashboard")


def home(request):
    packages = Package.objects.filter(is_active=True)
    # 获取按条售卖的商品
    line_packages = packages.filter(
        delivery_mode=Package.DELIVERY_STOCK,
        stock_mode=Package.STOCK_LINE,
    )
    # 按 available_stock_count 排序，获取库存最多的按条商品
    if line_packages.exists():
        line_package = sorted(line_packages, key=lambda p: p.available_stock_count, reverse=True)[0]
    else:
        line_package = None
    
    # 获取按组售卖的商品
    group_packages = packages.filter(
        delivery_mode=Package.DELIVERY_STOCK,
        stock_mode=Package.STOCK_GROUP,
    )
    # 按 available_stock_count 排序，获取库存最多的按组商品
    if group_packages.exists():
        group_package = sorted(group_packages, key=lambda p: p.available_stock_count, reverse=True)[0]
    else:
        group_package = None
    return render(
        request,
        "store/home.html",
        {
            "line_package": line_package,
            "group_package": group_package,
            "site_contact_config": _get_site_contact_config(),
        },
    )


def package_detail(request, pk):
    package = get_object_or_404(
        Package.objects.filter(is_active=True).prefetch_related("documents"),
        pk=pk,
    )
    
    # 获取不同 2FA 状态的库存和价格信息
    from store.models import StockItem
    twofa_statuses = {
        "no_2fa": {"label": "未开2fa", "count": 0, "price": package.price},
        "has_2fa": {"label": "已开通2fa", "count": 0, "price": package.price},
        "has_2fa_youtube": {"label": "已开通2fa可登录油管", "count": 0, "price": package.price},
    }
    
    # 计算不同 2FA 状态的库存数量
    if package.stock_mode == Package.STOCK_LINE:
        for status, info in twofa_statuses.items():
            # 计算当前商品的按条库存
            line_count = StockItem.objects.filter(
                package=package,
                is_sold=False,
                twofa_status=status
            ).count()
            # 计算组库存中可转换的数量
            group_count = 0
            group_packages = Package.objects.filter(
                stock_mode=Package.STOCK_GROUP,
                delivery_mode=Package.DELIVERY_STOCK,
                is_active=True
            )
            for group_package in group_packages:
                for stock_item in group_package.stock_items.filter(is_sold=False, twofa_status=status):
                    # 计算组内账号数量（排除分组标记行）
                    lines = [line.strip() for line in stock_item.content.splitlines() if line.strip() and "----" in line]
                    if lines:
                        # 减去1个主账号
                        group_count += max(0, len(lines) - 1)
            info["count"] = line_count + group_count
    else:
        # 按组售卖时，计算每组的 2FA 状态
        for stock_item in StockItem.objects.filter(
            package=package,
            is_sold=False
        ):
            # 使用库存项的twofa_status字段
            twofa_status = stock_item.twofa_status
            twofa_statuses[twofa_status]["count"] += 1
    
    quantity_options = range(1, package.available_unit_count + 1)
    return render(
        request,
        "store/package_detail.html",
        {
            "package": package,
            "quantity_options": quantity_options,
            "unit_label": _unit_label(package),
            "stock_label": _stock_label(package),
            "site_contact_config": _get_site_contact_config(),
            "twofa_statuses": twofa_statuses,
        },
    )


def inventory_status(request, pk):
    package = get_object_or_404(Package, pk=pk, is_active=True)
    return JsonResponse(
        {
            "available_unit_count": package.available_unit_count,
            "available_stock_count": package.available_stock_count,
            "unit_label": _unit_label(package),
            "stock_mode": package.stock_mode,
            "price": f"{package.price:.2f}",
        }
    )


def create_order(request, pk):
    if request.method != "POST":
        return redirect("package_detail", pk=pk)

    package = get_object_or_404(Package, pk=pk, is_active=True)
    return _create_order_from_request(
        request=request,
        package=package,
        sale_price=package.price,
    )


def order_detail(request, pk):
    order = get_object_or_404(Order.objects.select_related("package", "agent"), pk=pk)
    back_to_package_url = reverse("package_detail", args=[order.package.pk])
    back_to_home_url = reverse("home")
    if order.agent and order.agent_code_snapshot:
        back_to_package_url = reverse(
            "agent_package_detail",
            kwargs={"code": order.agent_code_snapshot, "pk": order.package.pk},
        )
        back_to_home_url = reverse("agent_home", kwargs={"code": order.agent_code_snapshot})
    context = {
        "order": order,
        "payment_ready": _payment_ready(),
        "pickup_expire_at": _pickup_expire_at(order),
        "unit_label": _unit_label(order.package),
        "back_to_package_url": back_to_package_url,
        "back_to_home_url": back_to_home_url,
    }
    return render(request, "store/order_detail.html", context)


def start_payment(request, pk):
    order = get_object_or_404(Order.objects.select_related("package"), pk=pk)
    if order.status == Order.STATUS_PAID:
        messages.success(request, "这个订单已经支付成功。")
        return redirect("order_detail", pk=order.pk)
    if order.status == Order.STATUS_ISSUE:
        messages.error(request, "这个订单已经支付成功，但当前正在人工处理，暂时不要重复支付。")
        return redirect("order_detail", pk=order.pk)
    if order.status == Order.STATUS_CLOSED:
        messages.error(request, "这个订单已关闭，不能再次发起支付。")
        return redirect("order_detail", pk=order.pk)

    if not _payment_ready():
        messages.error(request, "支付配置还没补完整，当前缺少签名配置。")
        return redirect("order_detail", pk=order.pk)

    order.payment_type = settings.CKKP_TYPE or ""
    order.save(update_fields=["payment_type"])

    payment_data = {
        "pid": settings.CKKP_PID,
        "out_trade_no": order.order_no,
        "notify_url": request.build_absolute_uri("/payments/ckkp/notify/"),
        "return_url": request.build_absolute_uri("/payments/ckkp/return/"),
        "name": order.package.name,
        "money": f"{order.amount:.2f}",
        "param": str(order.pk),
        "sign_type": settings.CKKP_SIGN_TYPE,
    }
    if settings.CKKP_TYPE:
        payment_data["type"] = settings.CKKP_TYPE
    payment_data["sign"] = sign_payload(
        payment_data,
        private_key_path=settings.CKKP_PRIVATE_KEY_PATH,
        sign_type=settings.CKKP_SIGN_TYPE,
        md5_key=settings.CKKP_MD5_KEY,
    )

    return render(
        request,
        "store/pay_redirect.html",
        {
            "gateway": settings.CKKP_GATEWAY,
            "payment_data": payment_data,
            "order": order,
        },
    )


def ckkp_notify(request):
    if request.method != "GET":
        return HttpResponse("fail")

    payload = request.GET.dict()
    if not _verify_ready():
        return HttpResponse("fail")
    if not verify_payload(
        payload,
        public_key_path=settings.CKKP_PLATFORM_PUBLIC_KEY_PATH,
        sign_type=settings.CKKP_SIGN_TYPE,
        md5_key=settings.CKKP_MD5_KEY,
    ):
        return HttpResponse("fail")
    if payload.get("trade_status") != "TRADE_SUCCESS":
        return HttpResponse("fail")

    order = Order.objects.filter(order_no=payload.get("out_trade_no", "")).first()
    if not order:
        return HttpResponse("fail")

    if payload.get("pid", "") != str(settings.CKKP_PID):
        return HttpResponse("fail")

    money = payload.get("money", "")
    if money:
        try:
            if Decimal(money) != order.amount:
                return HttpResponse("fail")
        except Exception:
            return HttpResponse("fail")

    result_status = _mark_order_paid(order, payload)

    return HttpResponse("success")


def ckkp_return(request):
    payload = request.GET.dict()
    order_no = request.GET.get("out_trade_no", "")
    order = Order.objects.filter(order_no=order_no).first()
    if not order:
        messages.error(request, "没有找到对应订单。")
        return redirect("home")

    if _verify_ready() and verify_payload(
        payload,
        public_key_path=settings.CKKP_PLATFORM_PUBLIC_KEY_PATH,
        sign_type=settings.CKKP_SIGN_TYPE,
        md5_key=settings.CKKP_MD5_KEY,
    ):
        if payload.get("pid", "") != str(settings.CKKP_PID):
            messages.error(request, "支付返回商户信息不匹配，请联系客服处理。")
            return redirect("order_detail", pk=order.pk)
        if payload.get("trade_status") == "TRADE_SUCCESS":
            result_status = _mark_order_paid(order, payload)
            if result_status == Order.STATUS_PAID:
                messages.success(request, "支付成功，请输入提取密码领取内容。")
                return redirect("pickup_order", pk=order.pk)
            if result_status == Order.STATUS_ISSUE:
                messages.error(request, "支付已完成，但当前库存不足，订单已转人工处理。")
                return redirect("order_detail", pk=order.pk)
        else:
            messages.error(request, f"支付状态异常：{payload.get('trade_status', '未知')}")
    else:
        messages.error(request, "支付返回验签失败，请稍后刷新订单状态。")
    return redirect("order_detail", pk=order.pk)


def pickup_order(request, pk):
    order = get_object_or_404(Order.objects.select_related("package", "agent"), pk=pk)
    delivered_items = []
    unlocked = False
    expired = _is_pickup_expired(order)

    if request.method == "POST":
        password = request.POST.get("pickup_password", "").strip()
        if order.status == Order.STATUS_ISSUE:
            messages.error(request, "订单已支付，但当前正在人工处理发货，请联系客服。")
        elif order.status != Order.STATUS_PAID:
            messages.error(request, "订单还没支付成功，暂时不能提取。")
        elif expired:
            messages.error(request, "这个订单的提取记录已超过 7 天。")
        elif not password or not check_password(password, order.pickup_password):
            messages.error(request, "提取密码不对。")
        else:
            unlocked = True
            delivered_items = _build_delivery_display_items(order.package, order.stock_items.all())
            messages.success(request, "提取成功，可以直接复制。")

    context = {
        "order": order,
        "delivered_items": delivered_items,
        "delivery_copy_text": _build_delivery_copy_text(
            order.package,
            [row["item"] for row in delivered_items],
        ) if delivered_items else "",
        "unlocked": unlocked,
        "expired": expired,
        "pickup_expire_at": _pickup_expire_at(order),
        "unit_label": _unit_label(order.package),
    }
    return render(request, "store/pickup_order.html", context)


def pickup_lookup(request):
    matched_orders = []
    unlocked = False

    if request.method == "POST":
        buyer_contact = request.POST.get("buyer_contact", "").strip()
        pickup_password = request.POST.get("pickup_password", "").strip()
        if not buyer_contact or not pickup_password:
            messages.error(request, "请填写联系方式和提取密码。")
        else:
            orders = list(
                Order.objects.select_related("package")
                .filter(
                    buyer_contact=buyer_contact,
                    status__in=[Order.STATUS_PAID, Order.STATUS_ISSUE],
                )
                .order_by("-paid_at", "-id")
            )
            valid_orders = [
                order
                for order in orders
                if order.status == Order.STATUS_PAID
                and order.pickup_password
                and check_password(pickup_password, order.pickup_password)
                and not _is_pickup_expired(order)
            ]
            issue_orders = [
                order
                for order in orders
                if order.pickup_password
                and check_password(pickup_password, order.pickup_password)
                and order.status == Order.STATUS_ISSUE
            ]
            if valid_orders:
                unlocked = True
                for order in valid_orders:
                    order_items = list(order.stock_items.all())
                    matched_orders.append(
                        {
                            "order": order,
                            "items": _build_delivery_display_items(order.package, order_items),
                            "delivery_copy_text": _build_delivery_copy_text(order.package, order_items),
                            "expire_at": _pickup_expire_at(order),
                        }
                    )
                messages.success(request, "提取成功，可以直接复制。")
            elif issue_orders:
                messages.error(request, "找到已支付订单，但当前库存不足，订单正在人工处理。")
            else:
                messages.error(request, "没有找到可提取的已支付订单，或提取密码不对。")

    return render(
        request,
        "store/pickup_lookup.html",
        {"matched_orders": matched_orders, "unlocked": unlocked},
    )


def _generate_order_no():
    return uuid4().hex[:24].upper()


def _build_delivery_display_items(package, stock_items):
    items = list(stock_items)
    total_count = len(items)
    display_items = []
    for index, item in enumerate(items, start=1):
        label = ""
        if total_count > 1:
            label = f"第{index}{'组' if package.stock_mode == Package.STOCK_GROUP else '条'}"
        display_items.append(
            {
                "item": item,
                "label": label,
                "copy_text": _build_delivery_copy_text(package, [item], include_labels=total_count > 1),
            }
        )
    return display_items


def _stock_item_inbox_url(item):
    return (getattr(item, "inbox_url", "") or "").strip() or MAIL_INBOX_URL


def _build_delivery_copy_text(package, stock_items, include_labels=False):
    items = list(stock_items)
    if not items:
        return ""
    inbox_urls = {_stock_item_inbox_url(item) for item in items}
    content_blocks = []
    for index, item in enumerate(items, start=1):
        block = item.content.strip()
        if include_labels and len(items) > 1:
            block = f"第{index}{'组' if package.stock_mode == Package.STOCK_GROUP else '条'}\n{block}"
        if len(inbox_urls) > 1:
            block = f"{block}\n{_build_delivery_instruction_text(package, _stock_item_inbox_url(item))}"
        content_blocks.append(block)
    if len(inbox_urls) == 1:
        instruction_block = _build_delivery_instruction_text(package, next(iter(inbox_urls)))
        return "\n\n".join(content_blocks + [instruction_block]).strip()
    return "\n\n".join(content_blocks).strip()


def _build_delivery_instruction_text(package, inbox_url=None):
    target_inbox_url = (inbox_url or "").strip() or MAIL_INBOX_URL
    if package.stock_mode == Package.STOCK_GROUP:
        lines = [
            f"接码网址：{target_inbox_url}",
            f"验证码登录：输入需要收验证码的邮箱，密码 {MAIL_INBOX_PASSWORD}。",
            "邮箱不要泄露。",
        ]
    else:
        lines = [
            f"接码网址：{target_inbox_url}",
            f"登录密码：{MAIL_INBOX_PASSWORD}",
            "哪条邮箱需要收验证码，就输入哪条邮箱。",
            "邮箱不要泄露。",
        ]
    return "\n".join(lines)


def _payment_ready():
    if settings.CKKP_SIGN_TYPE.upper() == "MD5":
        return bool(settings.CKKP_MD5_KEY and settings.CKKP_PID)
    return _private_key_ready() and _public_key_ready()


def _private_key_ready():
    return Path(settings.CKKP_PRIVATE_KEY_PATH).exists()


def _public_key_ready():
    return Path(settings.CKKP_PLATFORM_PUBLIC_KEY_PATH).exists()


def _verify_ready():
    if settings.CKKP_SIGN_TYPE.upper() == "MD5":
        return bool(settings.CKKP_MD5_KEY)
    return _public_key_ready()


def _mark_order_paid(order, payload):
    with transaction.atomic():
        locked_order = (
            Order.objects.select_for_update()
            .select_related("package")
            .get(pk=order.pk)
        )
        if locked_order.status in {Order.STATUS_PAID, Order.STATUS_ISSUE}:
            return locked_order.status

        locked_order.gateway_trade_no = payload.get("api_trade_no", "") or payload.get("trade_no", "")
        locked_order.paid_at = timezone.now()
        locked_order.payment_type = payload.get("type", locked_order.payment_type)

        if _allocate_stock_items(locked_order):
            locked_order.status = Order.STATUS_PAID
        else:
            locked_order.status = Order.STATUS_ISSUE

        locked_order.save(
            update_fields=["status", "gateway_trade_no", "paid_at", "payment_type"]
        )
        return locked_order.status


def _allocate_stock_items(order):
    if order.package.delivery_mode != Package.DELIVERY_STOCK:
        return True

    target_count = order.delivery_count
    allocated_count = order.stock_items.count()
    if allocated_count >= target_count:
        return True

    needed = target_count - allocated_count
    
    # 获取用户选择的 2FA 状态
    twofa_status = getattr(order, 'twofa_status', None)
    
    # 尝试从按条库存中分配指定 2FA 状态的账号
    filter_kwargs = {'package': order.package, 'is_sold': False}
    if twofa_status:
        filter_kwargs['twofa_status'] = twofa_status
    
    items = list(
        StockItem.objects.select_for_update()
        .filter(**filter_kwargs)
        .order_by("id")[:needed]
    )
    
    # 如果按条库存不足，且是按条售卖的商品，则从组库存中转换
    if len(items) < needed and order.package.stock_mode == Package.STOCK_LINE:
        remaining_needed = needed - len(items)
        
        # 查找所有活跃的组库存商品
        group_packages = Package.objects.filter(
            stock_mode=Package.STOCK_GROUP,
            delivery_mode=Package.DELIVERY_STOCK,
            is_active=True
        )
        
        # 收集所有组库存项，并按账号数量排序（从小到大）
        group_stock_items = []
        for group_package in group_packages:
            for stock_item in group_package.stock_items.filter(is_sold=False):
                lines = [line.strip() for line in stock_item.content.splitlines() if line.strip() and "----" in line]
                if lines and len(lines) > 1:  # 至少有主账号和一个子账号
                    group_stock_items.append((len(lines), stock_item))
        
        # 按账号数量从小到大排序
        group_stock_items.sort(key=lambda x: x[0])
        
        # 从组库存中转换账号
        for _, group_item in group_stock_items:
            if remaining_needed <= 0:
                break
            
            # 提取组内的账号
            lines = [line.strip() for line in group_item.content.splitlines() if line.strip() and "----" in line]
            if len(lines) > 1:
                # 保留主账号，提取子账号
                child_accounts = lines[1:]
                # 计算可提取的子账号数量
                extract_count = min(remaining_needed, len(child_accounts))
                
                # 创建新的按条库存
                for i in range(extract_count):
                    new_stock_item = StockItem(
                        package=order.package,
                        content=child_accounts[i],
                        inbox_url=group_item.inbox_url,
                        twofa_status=group_item.twofa_status  # 使用组库存项的实际 2FA 状态
                    )
                    new_stock_item.save()
                    items.append(new_stock_item)
                
                remaining_needed -= extract_count
                
                # 更新组库存内容（移除已提取的子账号）
                if len(child_accounts) > extract_count:
                    # 保留主账号和剩余子账号
                    new_content = lines[0] + "\n" + "\n".join(child_accounts[extract_count:])
                    group_item.content = new_content
                    group_item.save(update_fields=["content"])
                else:
                    # 所有子账号都被提取，删除该组库存
                    group_item.delete()
    
    if len(items) < needed:
        return False

    now = timezone.now()
    for item in items:
        item.is_sold = True
        item.sold_order = order
        item.sold_at = now
        item.save(update_fields=["is_sold", "sold_order", "sold_at"])
    return True


def _parse_quantity(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _pickup_expire_at(order):
    if not order.paid_at:
        return None
    return order.paid_at + timedelta(days=7)


def _site_base_url(request):
    return f"{request.scheme}://{request.get_host()}"


def _is_pickup_expired(order):
    expire_at = _pickup_expire_at(order)
    if not expire_at:
        return False
    return timezone.now() > expire_at


def _unit_label(package):
    if package.delivery_mode != Package.DELIVERY_STOCK:
        return "份"
    if package.stock_mode == Package.STOCK_GROUP:
        return "组"
    return "条"


def _stock_label(package):
    if package.delivery_mode != Package.DELIVERY_STOCK:
        return "份"
    if package.stock_mode == Package.STOCK_GROUP:
        return "组"
    return "条"
