# Базовый образ с нужной версией Python
FROM python:3.10-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем зависимости системы
RUN apt-get update && apt-get install -y gcc libffi-dev

# Копируем файлы проекта в контейнер
COPY . .

# Устанавливаем зависимости Python
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Указываем переменные окружения
ENV PYTHONUNBUFFERED=1

# Запускаем бота
CMD ["python", "bot.py"]
