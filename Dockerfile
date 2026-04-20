# Apify's official Python + Playwright base image (Chromium included)
FROM apify/actor-python-playwright:3.11

# Copy dependency list first (better Docker layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium browser
RUN playwright install chromium

# Copy actor source code
COPY src/ ./src/
COPY .actor/ ./.actor/

# Make src/ importable as a package
ENV PYTHONPATH="/usr/src/app/src:${PYTHONPATH}"

# Entry point
CMD ["python", "src/main.py"]
