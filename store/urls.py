from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("agent/apply/", views.agent_apply, name="agent_apply"),
    path("agent/email-code/", views.agent_send_email_code, name="agent_send_email_code"),
    path("agent/login/", views.agent_login, name="agent_login"),
    path("agent/logout/", views.agent_logout, name="agent_logout"),
    path("agent/dashboard/", views.agent_dashboard, name="agent_dashboard"),
    path("agent/bind-email/", views.agent_bind_email, name="agent_bind_email"),
    path("agent/profile/", views.agent_update_profile, name="agent_update_profile"),
    path("agent/password/", views.agent_update_password, name="agent_update_password"),
    path("agent/password-reset/", views.agent_password_reset, name="agent_password_reset"),
    path("agent/prices/", views.agent_update_prices, name="agent_update_prices"),
    path("a/<str:code>/", views.agent_public_home, name="agent_home"),
    path("a/<str:code>/packages/<int:pk>/", views.agent_package_detail, name="agent_package_detail"),
    path("a/<str:code>/packages/<int:pk>/buy/", views.agent_create_order, name="agent_create_order"),
    path("google-mail/", views.seo_page, {"slug": "google-mail"}, name="seo_google_mail"),
    path("enterprise-google-mail/", views.seo_page, {"slug": "enterprise-google-mail"}, name="seo_enterprise_google_mail"),
    path("google-mail-buy/", views.seo_page, {"slug": "google-mail-buy"}, name="seo_google_mail_buy"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap_xml"),
    path("pickup/", views.pickup_lookup, name="pickup_lookup"),
    path("packages/<int:pk>/", views.package_detail, name="package_detail"),
    path("packages/<int:pk>/inventory/", views.inventory_status, name="inventory_status"),
    path("packages/<int:pk>/buy/", views.create_order, name="create_order"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("orders/<int:pk>/pickup/", views.pickup_order, name="pickup_order"),
    path("orders/<int:pk>/pay/", views.start_payment, name="start_payment"),
    path("payments/ckkp/notify/", views.ckkp_notify, name="ckkp_notify"),
    path("payments/ckkp/return/", views.ckkp_return, name="ckkp_return"),
]
