import os
import subprocess
import requests
import sys

gitlab_token = os.environ.get('GITLAB_TOKEN')
gitlab_project_id = os.environ.get('CI_PROJECT_ID')
gitlab_mr_iid = os.environ.get('CI_MERGE_REQUEST_IID')
gitlab_api_url = os.environ.get('CI_API_V4_URL', 'https://gitlab.com/api/v4')
gemini_api_key = os.environ.get('GEMINI_API_KEY')


def get_mr_changes():
    url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}/changes"
    headers = {"PRIVATE-TOKEN": gitlab_token}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()['changes']


def read_prompt(prompt_path):
    with open(prompt_path, 'r', encoding='utf-8') as f:
        return f.read().strip()


def review_with_gemini(diff_text, prompt_text):
    # 프롬프트 파일에서 읽은 내용과 diff를 합쳐 전달
    prompt = f"{prompt_text}\n\n{diff_text}"
    result = subprocess.run(
        ["gemini", "review", "--api-key", gemini_api_key],
        input=prompt.encode(),
        capture_output=True,
        check=True
    )
    return result.stdout.decode()


def post_mr_comment(body):
    url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}/notes"
    headers = {"PRIVATE-TOKEN": gitlab_token}
    data = {"body": body}
    resp = requests.post(url, headers=headers, data=data)
    resp.raise_for_status()
    return resp.json()


def main():
    prompt_path = sys.argv[1] if len(sys.argv) > 1 else "prompt.txt"
    prompt_text = read_prompt(prompt_path)
    changes = get_mr_changes()
    for change in changes:
        diff = change.get('diff')
        if not diff:
            continue
        filename = change.get('new_path')
        review = review_with_gemini(diff, prompt_text)
        comment = f"### Gemini 코드리뷰: `{filename}`\n\n{review}"
        post_mr_comment(comment)

if __name__ == "__main__":
    main()
