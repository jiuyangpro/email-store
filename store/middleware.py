import re

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse


class SimpleRateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.rules = [
            {
                **rule,
                "pattern": re.compile(rule["pattern"]),
                "methods": {method.upper() for method in rule.get("methods", ["POST"])},
            }
            for rule in getattr(settings, "APP_RATE_LIMIT_RULES", [])
        ]

    def __call__(self, request):
        client_ip = self._get_client_ip(request)
        path = request.path
        method = request.method.upper()

        for rule in self.rules:
            if method not in rule["methods"]:
                continue
            if not rule["pattern"].match(path):
                continue

            cache_key = f"rate-limit:{rule['name']}:{client_ip}"
            count = self._increase_counter(cache_key, rule["window"])
            if count > rule["limit"]:
                return HttpResponse(
                    f"操作太频繁了，请 {rule['block_message']} 后再试。",
                    status=429,
                    content_type="text/plain; charset=utf-8",
                )

        return self.get_response(request)

    def _increase_counter(self, cache_key, timeout):
        if cache.add(cache_key, 1, timeout=timeout):
            return 1
        try:
            return cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, 1, timeout=timeout)
            return 1

    def _get_client_ip(self, request):
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
