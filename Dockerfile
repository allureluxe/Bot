FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scalper/ ./scalper/
COPY run_scalper.py .

# L'etat (positions, journal) doit survivre aux redemarrages du conteneur :
#   docker run -v $(pwd)/state:/app/state --env-file .env scalping-bot
VOLUME ["/app/state"]

ENV PYTHONUNBUFFERED=1

CMD ["python", "run_scalper.py"]
