import os
import requests
import sys
import subprocess
import tempfile
import json

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


def review_with_gemini_cli(diff_text, prompt_text):
    """Gemini CLI를 사용하여 코드 리뷰 생성"""
    full_prompt = f"{prompt_text}\n\n{diff_text}"

    try:
        # Gemini CLI는 GEMINI_API_KEY 환경변수를 사용
        env = os.environ.copy()
        env['GEMINI_API_KEY'] = gemini_api_key

        # 올바른 Gemini CLI 명령어 구조
        cmd = ['gemini', '--prompt', full_prompt]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=120,
            env=env
        )

        if result.returncode == 0:
            return result.stdout.strip()
        else:
            error_msg = result.stderr.strip() if result.stderr else "알 수 없는 오류"
            print(f"Gemini CLI 오류 (종료 코드 {result.returncode}): {error_msg}")

            # 표준 입력으로 프롬프트 전달 시도
            try:
                print("표준 입력 방식으로 재시도 중...")
                cmd_stdin = ['gemini']
                result_stdin = subprocess.run(
                    cmd_stdin,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    timeout=120,
                    env=env
                )

                if result_stdin.returncode == 0:
                    return result_stdin.stdout.strip()
                else:
                    return f"❌ Gemini CLI 실행 실패: {error_msg}"
            except Exception:
                return f"❌ Gemini CLI 실행 실패: {error_msg}"

    except subprocess.TimeoutExpired:
        print("Gemini CLI 실행 시간 초과")
        return "❌ Gemini CLI 실행 시간이 초과되었습니다."
    except FileNotFoundError:
        print("Gemini CLI를 찾을 수 없습니다. 설치되어 있는지 확인해주세요.")
        return "❌ Gemini CLI가 설치되어 있지 않습니다."
    except Exception as e:
        print(f"Gemini CLI 실행 중 예상치 못한 오류: {e}")
        return f"❌ 예상치 못한 오류: {str(e)}"


def post_mr_comment(body):
    url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}/notes"
    headers = {"PRIVATE-TOKEN": gitlab_token}
    data = {"body": body}
    resp = requests.post(url, headers=headers, data=data)
    resp.raise_for_status()
    return resp.json()


def main():
    try:
        # 필수 환경변수 체크
        if not all([gitlab_token, gitlab_project_id, gitlab_mr_iid, gemini_api_key]):
            missing_vars = []
            if not gitlab_token: missing_vars.append("GITLAB_TOKEN")
            if not gitlab_project_id: missing_vars.append("CI_PROJECT_ID")
            if not gitlab_mr_iid: missing_vars.append("CI_MERGE_REQUEST_IID")
            if not gemini_api_key: missing_vars.append("GEMINI_API_KEY")

            print(f"필수 환경변수가 설정되지 않았습니다: {', '.join(missing_vars)}")
            sys.exit(1)

        prompt_path = sys.argv[1] if len(sys.argv) > 1 else "prompt.txt"

        # 프롬프트 파일 존재 확인
        if not os.path.exists(prompt_path):
            print(f"프롬프트 파일을 찾을 수 없습니다: {prompt_path}")
            sys.exit(1)

        prompt_text = read_prompt(prompt_path)

        # MR 변경사항 가져오기
        try:
            changes = get_mr_changes()
        except requests.exceptions.RequestException as e:
            print(f"GitLab API 호출 오류: {e}")
            sys.exit(1)

        if not changes:
            print("리뷰할 변경사항이 없습니다.")
            return

        print(f"📝 {len(changes)}개 파일에 대한 리뷰를 시작합니다...")

        # 각 파일별로 리뷰 수행
        for i, change in enumerate(changes, 1):
            diff = change.get('diff')
            if not diff:
                continue

            filename = change.get('new_path') or change.get('old_path', 'unknown')
            print(f"🔍 [{i}/{len(changes)}] 리뷰 중: {filename}")

            try:
                review = review_with_gemini_cli(diff, prompt_text)
                comment = f"### 🤖 Gemini 코드리뷰: `{filename}`\n\n{review}"
                post_mr_comment(comment)
                print(f"✅ {filename} 리뷰 완료")

            except Exception as e:
                print(f"❌ {filename} 리뷰 실패: {e}")
                # 개별 파일 실패 시에도 계속 진행
                continue

        print("🎉 모든 파일 리뷰가 완료되었습니다!")

    except KeyboardInterrupt:
        print("\n⏹️ 사용자에 의해 중단되었습니다.")
        sys.exit(1)
    except Exception as e:
        print(f"💥 예상치 못한 오류 발생: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
