#!/bin/sh
# 告警检查的包装脚本
#
# 为什么要多这一层：
#   crontab 行里直接写长路径 + 重定向（>> ... 2>&1）在某些自动化下发通道里
#   会被拦截或截断；把重定向收进脚本内部，crontab 行就只剩一个短路径，
#   既好维护也避免转义问题。
exec /opt/research-reports/deploy/alert.py >> /var/log/research-reports/alert.log 2>&1
