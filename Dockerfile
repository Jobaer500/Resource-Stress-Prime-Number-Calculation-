FROM python:3.11-slim

WORKDIR /app

COPY resource_stress.py .

EXPOSE 8080

CMD ["python3", "resource_stress.py"]
