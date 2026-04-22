#!/bin/bash

# 进入项目目录
cd /var/www/docstore

# 激活虚拟环境
source venv/bin/activate

# 检查超级用户
python manage.py shell -c "from django.contrib.auth.models import User; print('超级用户列表:', User.objects.filter(is_superuser=True).values_list('username', flat=True))"
