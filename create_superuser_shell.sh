#!/bin/bash

# 进入项目目录
cd /var/www/docstore

# 激活虚拟环境
source venv/bin/activate

# 使用 Django shell 创建超级用户
python manage.py shell -c "from django.contrib.auth.models import User; user = User.objects.create_superuser('admin', 'admin@example.com', 'admin123'); user.save(); print('超级用户创建成功！')"
