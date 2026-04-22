#!/bin/bash

# 进入项目目录
cd /var/www/docstore

# 激活虚拟环境
source venv/bin/activate

# 检查是否存在用户名 admin，如果存在就更新密码，否则创建新的超级用户
python manage.py shell -c "from django.contrib.auth.models import User; user, created = User.objects.get_or_create(username='admin', defaults={'email': 'admin@example.com', 'is_superuser': True, 'is_staff': True}); user.set_password('admin123'); user.save(); print('超级用户密码更新成功！' if not created else '超级用户创建成功！')"
