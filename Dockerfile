FROM python:3.11-slim

# 필수 패키지 설치
RUN pip install --no-cache-dir requests

# gemini cli 설치 (예시: npm 사용, 실제 설치법은 gemini 공식 문서 참고)
RUN apt-get update && \
    apt-get install -y npm && \
    npm install -g @google/gemini-cli && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY reviewer.py ./

ENTRYPOINT ["python", "reviewer.py"]
