#!/bin/bash

# 启动 Gunicorn 服务
systemctl start gunicorn

# 检查 Gunicorn 服务状态
systemctl status gunicorn
