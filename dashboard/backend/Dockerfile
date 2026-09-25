FROM python:3.11-slim

WORKDIR /app

COPY dashboard/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard/backend/main.py dashboard/backend/security.py ./
COPY deploy_demo/__init__.py deploy_demo/__main__.py deploy_demo/deployment_spec.py \
    deploy_demo/fleet_state.py ./deploy_demo/

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
