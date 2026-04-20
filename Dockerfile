FROM apify/actor-python-playwright:3.11

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium

COPY src/ ./src/
COPY .actor/ ./.actor/

ENV PYTHONPATH="/usr/src/app/src:${PYTHONPATH}"

CMD ["python", "src/main.py"]
