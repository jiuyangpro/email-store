#!/bin/bash

# 进入项目目录
cd /var/www/docstore

# 激活虚拟环境
source venv/bin/activate

# 创建超级用户
python manage.py createsuperuser --username admin --email admin@example.com --noinput

# 输出结果
echo "超级用户创建成功！"
echo "用户名: admin"
echo "密码: admin123"
