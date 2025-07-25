# GitLab MR Gemini Auto Reviewer

## 개요

이 프로젝트는 Gemini CLI를 활용해 GitLab Merge Request(MR) 코드리뷰를 자동화하는 도구입니다.

## 사용 방법

### 1. prompt.txt 작성

리뷰에 사용할 프롬프트를 프로젝트 루트에 `prompt.txt` 파일로 작성하세요.

예시:
```
아래 diff는 GitLab Merge Request의 변경사항입니다. 코드리뷰를 해주세요.
- 개선점, 버그, 보안 이슈, 스타일 등을 지적해 주세요.
- 친절하고 구체적으로 설명해 주세요.
```

### 2. GitLab CI 설정 예시 (`.gitlab-ci.yml`)

```yaml
image:
  name: ghcr.io/sinna94/gitlab-mr-gemini-reviewer:latest
  entrypoint: [""]

stages:
  - review

review-mr:
  stage: review
  script:
    - python /app/reviewer.py prompt.txt
  only:
    - merge_requests
  variables:
    GITLAB_TOKEN: "$GITLAB_PAT"  # GitLab Personal Access Token (환경변수에 등록 필요)
    GEMINI_API_KEY: "$GEMINI_API_KEY"  # 환경변수에 Gemini API Key 등록 필요
```

- `GEMINI_API_KEY`는 GitLab CI/CD 환경변수에 등록해야 합니다.
- MR이 생성/업데이트될 때마다 자동으로 코드리뷰가 코멘트로 등록됩니다.
- 각 프로젝트 루트에 `prompt.txt` 파일이 있어야 합니다.

### 3. 환경변수 설정
- `GITLAB_PAT`: GitLab Personal Access Token (`api`, `read_repository` 권한 필요)
- `GEMINI_API_KEY`: Gemini CLI API Key

#### GitLab Personal Access Token 생성 방법:
1. GitLab → Settings → Access Tokens
2. Token name: `mr-reviewer` (원하는 이름)
3. Scopes: `api`, `read_repository` 선택
4. 생성된 토큰을 GitLab CI/CD 환경변수 `GITLAB_PAT`에 등록

---

자세한 사용법 및 커스터마이징은 `reviewer.py`와 `prompt.txt`를 참고하세요.
