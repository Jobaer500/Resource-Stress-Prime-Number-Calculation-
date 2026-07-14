FROM python:3.11-slim

WORKDIR /app

COPY resource_stress.py .

CMD ["python3", "resource_stress.py"]
