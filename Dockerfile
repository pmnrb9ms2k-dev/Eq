# 使用轻量级 Python 镜像
FROM python:3.11-slim

# 创建非 root 用户（Hugging Face 的安全要求）
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

# 设置工作目录
WORKDIR /app

# 安装 Python 依赖
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY --chown=user . .

# 暴露 Hugging Face Spaces 要求的 7860 端口
EXPOSE 7860

# 启动命令
CMD ["python", "main.py"]
