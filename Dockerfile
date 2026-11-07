FROM python:3.12-alpine
WORKDIR /app
COPY server.py index.html config.json ./
EXPOSE 80
CMD ["python", "server.py"]
