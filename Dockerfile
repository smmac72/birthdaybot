FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY birthday_bot.py .

VOLUME ["/app/data"]

# run cmd
CMD ["python", "birthday_bot.py"]