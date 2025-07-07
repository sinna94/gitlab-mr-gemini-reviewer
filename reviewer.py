import os
import requests
import sys
import subprocess
import tempfile
import json
import urllib.parse
import re
import ast
from pathlib import Path

gitlab_token = os.environ.get('GITLAB_TOKEN')
gitlab_project_id = os.environ.get('CI_PROJECT_ID')
gitlab_mr_iid = os.environ.get('CI_MERGE_REQUEST_IID')
gitlab_api_url = os.environ.get('CI_API_V4_URL', 'https://gitlab.com/api/v4')
gemini_api_key = os.environ.get('GEMINI_API_KEY')


def get_latest_commit_changes():
    """최신 커밋에서 변경된 파일들만 가져오기"""
    # 최신 커밋 SHA 가져오기
    commits_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/repository/commits"
    headers = {"PRIVATE-TOKEN": gitlab_token}

    # MR의 source branch에서 최신 커밋 가져오기
    mr_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}"
    mr_resp = requests.get(mr_url, headers=headers)
    mr_resp.raise_for_status()
    mr_data = mr_resp.json()

    source_branch = mr_data['source_branch']

    # source branch의 최신 커밋 정보 가져오기
    encoded_branch = urllib.parse.quote(source_branch, safe='')
    branch_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/repository/branches/{encoded_branch}"
    branch_resp = requests.get(branch_url, headers=headers)
    branch_resp.raise_for_status()
    latest_commit_sha = branch_resp.json()['commit']['id']

    # 최신 커밋의 변경사항 가져오기
    commit_diff_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/repository/commits/{latest_commit_sha}/diff"
    diff_resp = requests.get(commit_diff_url, headers=headers)
    diff_resp.raise_for_status()

    return diff_resp.json(), latest_commit_sha


def get_mr_changes():
    """MR 전체 변경사항 가져오기 (기존 방식)"""
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


def has_been_reviewed_before(commit_sha):
    """이전에 리뷰했던 커밋인지 확인"""
    # MR의 기존 노트들을 확인해서 해당 커밋이 이미 리뷰되었는지 체크
    notes_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}/notes"
    headers = {"PRIVATE-TOKEN": gitlab_token}

    try:
        resp = requests.get(notes_url, headers=headers)
        resp.raise_for_status()
        notes = resp.json()

        # 커밋 SHA가 포함된 리뷰 댓글이 있는지 확인
        review_marker = f"<!-- REVIEWED_COMMIT:{commit_sha} -->"
        for note in notes:
            if review_marker in note.get('body', ''):
                return True
        return False
    except:
        # 에러 발생 시 안전하게 False 반환 (새로 리뷰)
        return False

def advanced_group_related_files(changes):
    """고급 파일 그룹핑 - 사용 관계, 테스트, 설정 파일 등을 모두 고려"""
    groups = []
    processed = set()

    # 파일별 분석 정보 저장
    file_analysis = {}

    # 1단계: 모든 파일 분석
    for change in changes:
        file_path = change.get('new_path') or change.get('old_path')
        if file_path:
            file_analysis[file_path] = analyze_file(change, file_path)

    # 2단계: 관계 매핑 생성
    relationships = build_relationship_map(file_analysis, changes)

    # 3단계: 그룹 생성
    for change in changes:
        file_path = change.get('new_path') or change.get('old_path')
        if file_path in processed:
            continue

        # 해당 파일과 관련된 모든 파일 찾기
        related_files = find_all_related_files(file_path, relationships, changes)

        # 그룹 생성
        group = create_file_group(file_path, related_files, file_analysis)
        groups.append(group)

        # 처리된 파일들 마킹
        for rf in related_files:
            processed.add(rf.get('new_path') or rf.get('old_path'))

    return groups

def detect_language(file_path):
    """파일 확장자로 언어 감지"""
    if file_path.endswith('.java'):
        return 'java'
    elif file_path.endswith('.kt'):
        return 'kotlin'
    elif file_path.endswith('.py'):
        return 'python'
    elif file_path.endswith('.go'):
        return 'go'
    elif file_path.endswith(('.ts', '.tsx')):
        return 'typescript'
    elif file_path.endswith(('.js', '.jsx')):
        return 'javascript'
    elif file_path.endswith('.cs'):
        return 'csharp'
    elif file_path.endswith('.rb'):
        return 'ruby'
    elif file_path.endswith('.php'):
        return 'php'
    elif file_path.endswith(('.cpp', '.cc', '.cxx')):
        return 'cpp'
    elif file_path.endswith('.c'):
        return 'c'
    elif file_path.endswith('.rs'):
        return 'rust'
    elif file_path.endswith('.swift'):
        return 'swift'
    elif file_path.endswith('.scala'):
        return 'scala'
    else:
        return 'unknown'

def is_test_file(file_path):
    """테스트 파일인지 확인"""
    path_lower = file_path.lower()

    # 테스트 디렉토리 패턴
    test_dirs = ['test/', 'tests/', '__test__/', '__tests__/', 'spec/', 'specs/']
    for test_dir in test_dirs:
        if test_dir in path_lower:
            return True

    # 테스트 파일명 패턴
    test_patterns = [
        'test', 'tests', 'spec', 'specs', '_test', '_tests',
        '.test.', '.spec.', 'test_', 'spec_'
    ]

    for pattern in test_patterns:
        if pattern in path_lower:
            return True

    return False

def analyze_file(change, file_path):
    """파일 분석 - 타입, 의존성, 용도 등"""
    analysis = {
        'type': determine_file_type(file_path),
        'language': detect_language(file_path),
        'imports': [],
        'exports': [],
        'classes': [],
        'functions': [],
        'is_test': is_test_file(file_path),
        'is_config': is_config_file(file_path),
        'is_doc': is_documentation_file(file_path),
        'dependencies': []
    }

    # diff에서 imports/exports 추출
    diff_content = change.get('diff', '')
    if diff_content:
        analysis.update(extract_dependencies_from_diff(diff_content, analysis['language']))

    return analysis

def determine_file_type(file_path):
    """파일 타입 결정"""
    path_lower = file_path.lower()

    if any(test_indicator in path_lower for test_indicator in ['test', 'spec', '__test__']):
        return 'test'
    elif any(config_indicator in path_lower for config_indicator in ['config', 'setting', 'env', 'properties']):
        return 'config'
    elif any(doc_indicator in path_lower for doc_indicator in ['readme', 'doc', 'md', 'rst']):
        return 'documentation'
    elif file_path.endswith(('.py', '.java', '.kt', '.ts', '.js', '.go', '.cs')):
        return 'source'
    elif file_path.endswith(('.json', '.yaml', '.yml', '.xml', '.toml')):
        return 'config'
    elif file_path.endswith(('.sql', '.migration')):
        return 'database'
    else:
        return 'other'

def is_config_file(file_path):
    """설정 파일 여부 확인"""
    config_patterns = [
        'config', 'setting', 'env', 'properties', 'application.yml',
        'application.yaml', 'application.properties', 'pom.xml',
        'build.gradle', 'package.json', 'requirements.txt', 'Dockerfile'
    ]
    return any(pattern in file_path.lower() for pattern in config_patterns)

def is_documentation_file(file_path):
    """문서 파일 여부 확인"""
    return file_path.lower().endswith(('.md', '.rst', '.txt', '.adoc')) or 'readme' in file_path.lower()

def extract_dependencies_from_diff(diff_content, language):
    """diff에서 의존성 추출"""
    dependencies = {
        'imports': [],
        'exports': [],
        'classes': [],
        'functions': []
    }

    # 추가된 라인만 분석 (+ 로 시작하는 라인)
    added_lines = [line[1:] for line in diff_content.split('\n') if line.startswith('+') and not line.startswith('+++')]

    for line in added_lines:
        line = line.strip()

        if language == 'python':
            # Python imports
            if line.startswith('import ') or line.startswith('from '):
                dependencies['imports'].append(line)
            # Python class/function definitions
            elif line.startswith('class '):
                match = re.match(r'class\s+(\w+)', line)
                if match:
                    dependencies['classes'].append(match.group(1))
            elif line.startswith('def '):
                match = re.match(r'def\s+(\w+)', line)
                if match:
                    dependencies['functions'].append(match.group(1))

        elif language in ['java', 'kotlin']:
            # Java/Kotlin imports
            if line.startswith('import '):
                dependencies['imports'].append(line)
            # Class definitions
            elif 'class ' in line:
                match = re.search(r'class\s+(\w+)', line)
                if match:
                    dependencies['classes'].append(match.group(1))

        elif language in ['javascript', 'typescript']:
            # JS/TS imports
            if line.startswith('import ') or 'require(' in line:
                dependencies['imports'].append(line)
            # Exports
            elif line.startswith('export '):
                dependencies['exports'].append(line)
            # Class/function definitions
            elif 'class ' in line:
                match = re.search(r'class\s+(\w+)', line)
                if match:
                    dependencies['classes'].append(match.group(1))

    return dependencies

def build_relationship_map(file_analysis, changes):
    """파일 간 관계 매핑 생성"""
    relationships = {}

    for file_path, analysis in file_analysis.items():
        relationships[file_path] = {
            'tests': [],
            'tested_by': [],
            'imports': [],
            'imported_by': [],
            'configs': [],
            'configured_by': [],
            'documents': [],
            'documented_by': []
        }

    # 관계 분석
    for file_path, analysis in file_analysis.items():
        for other_path, other_analysis in file_analysis.items():
            if file_path == other_path:
                continue

            # 테스트 관계
            if is_test_relationship(file_path, other_path, analysis, other_analysis):
                if analysis['is_test']:
                    relationships[file_path]['tests'].append(other_path)
                    relationships[other_path]['tested_by'].append(file_path)
                else:
                    relationships[file_path]['tested_by'].append(other_path)
                    relationships[other_path]['tests'].append(file_path)

            # import 관계
            if is_import_relationship(file_path, other_path, analysis, other_analysis):
                relationships[file_path]['imports'].append(other_path)
                relationships[other_path]['imported_by'].append(file_path)

            # 설정 관계
            if is_config_relationship(file_path, other_path, analysis, other_analysis):
                if analysis['is_config']:
                    relationships[file_path]['configured_by'].append(other_path)
                    relationships[other_path]['configs'].append(file_path)
                else:
                    relationships[file_path]['configs'].append(other_path)
                    relationships[other_path]['configured_by'].append(file_path)

    return relationships

def is_test_relationship(file1, file2, analysis1, analysis2):
    """테스트 관계 여부 확인"""
    # 파일명 기반 매칭
    base1 = Path(file1).stem.lower()
    base2 = Path(file2).stem.lower()

    # 테스트 접미사/접두사 제거
    test_patterns = ['test', 'tests', 'spec', 'specs']
    for pattern in test_patterns:
        base1 = base1.replace(pattern, '')
        base2 = base2.replace(pattern, '')

    # 클래스명 매칭
    if analysis1['classes'] and analysis2['classes']:
        for class1 in analysis1['classes']:
            for class2 in analysis2['classes']:
                if class1.lower().replace('test', '') == class2.lower().replace('test', ''):
                    return True

    return base1 == base2

def is_import_relationship(file1, file2, analysis1, analysis2):
    """import 관계 여부 확인"""
    # 파일명이 import 구문에 포함되는지 확인
    file2_name = Path(file2).stem

    for import_line in analysis1['imports']:
        if file2_name.lower() in import_line.lower():
            return True

    return False

def is_config_relationship(file1, file2, analysis1, analysis2):
    """설정 관계 여부 확인"""
    # 설정 파일과 소스 파일 관계
    if analysis1['is_config'] and analysis2['type'] == 'source':
        return True
    elif analysis2['is_config'] and analysis1['type'] == 'source':
        return True

    return False

def find_all_related_files(file_path, relationships, changes):
    """파일과 관련된 모든 파일 찾기"""
    related_paths = set()

    # 직접 관계
    for rel_type, related_list in relationships.get(file_path, {}).items():
        related_paths.update(related_list)

    # 간접 관계 (2차 연결)
    for related_path in list(related_paths):
        for rel_type, indirect_list in relationships.get(related_path, {}).items():
            if rel_type in ['tests', 'tested_by', 'imports', 'imported_by']:
                related_paths.update(indirect_list)

    # 변경된 파일들만 필터링
    change_paths = {change.get('new_path') or change.get('old_path') for change in changes}
    related_paths = related_paths.intersection(change_paths)

    # Change 객체 반환
    related_files = []
    for change in changes:
        change_path = change.get('new_path') or change.get('old_path')
        if change_path in related_paths or change_path == file_path:
            related_files.append(change)

    return related_files

def create_file_group(main_file, related_files, file_analysis):
    """파일 그룹 생성"""
    main_analysis = file_analysis.get(main_file, {})

    # 그룹 타입 결정
    file_types = [file_analysis.get(f.get('new_path') or f.get('old_path'), {}).get('type', 'other')
                  for f in related_files]

    has_test = 'test' in file_types
    has_source = 'source' in file_types
    has_config = 'config' in file_types
    has_doc = 'documentation' in file_types

    if has_test and has_source and has_config:
        group_type = 'comprehensive'
    elif has_test and has_source:
        group_type = 'source_with_test'
    elif has_source and has_config:
        group_type = 'source_with_config'
    elif has_test:
        group_type = 'test_only'
    elif has_config:
        group_type = 'config_only'
    elif has_doc:
        group_type = 'documentation_only'
    else:
        group_type = 'source_only'

    return {
        'type': group_type,
        'files': related_files,
        'main_file': main_file,
        'language': main_analysis.get('language', 'unknown'),
        'summary': create_group_summary(related_files, file_analysis)
    }

def create_group_summary(related_files, file_analysis):
    """그룹 요약 생성"""
    file_info = []
    for file_change in related_files:
        file_path = file_change.get('new_path') or file_change.get('old_path')
        analysis = file_analysis.get(file_path, {})
        file_info.append({
            'path': file_path,
            'type': analysis.get('type', 'unknown'),
            'classes': analysis.get('classes', []),
            'functions': analysis.get('functions', [])
        })

    return file_info

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

        # 최신 커밋의 변경사항만 가져오기
        try:
            changes, latest_commit_sha = get_latest_commit_changes()
        except requests.exceptions.RequestException as e:
            print(f"GitLab API 호출 오류: {e}")
            sys.exit(1)

        if not changes:
            print("리뷰할 변경사항이 없습니다.")
            return

        # 이미 리뷰한 커밋인지 확인
        if has_been_reviewed_before(latest_commit_sha):
            print(f"커밋 {latest_commit_sha[:8]}은 이미 리뷰되었습니다.")
            print("새로운 커밋을 푸시하면 해당 변경사항만 리뷰됩니다.")
            return

        # 고급 파일 그룹핑 사용
        file_groups = advanced_group_related_files(changes)

        print(f"📝 커밋 {latest_commit_sha[:8]}의 {len(file_groups)}개 복합 그룹에 대한 리뷰를 시작합니다...")

        for i, group in enumerate(file_groups, 1):
            group_type = group['type']
            main_file = group['main_file']
            files = group['files']
            summary = group['summary']

            print(f"🔍 [{i}/{len(file_groups)}] 리뷰 중: {main_file} ({group_type})")

            # 모든 관련 파일의 diff를 합쳐서 컨텍스트 제공
            combined_diff = ""
            file_details = []

            for file_change in files:
                diff = file_change.get('diff')
                if diff:
                    filename = file_change.get('new_path') or file_change.get('old_path', 'unknown')
                    file_details.append(filename)
                    combined_diff += f"\n### 파일: {filename}\n{diff}\n"

            if not combined_diff:
                continue

            # 그룹 컨텍스트 생성
            context_info = f"""
📋 **파일 그룹 정보**:
- 그룹 타입: {group_type}
- 포함 파일: {', '.join(file_details)}
- 주요 언어: {group['language']}

📊 **파일별 상세**:
"""
            for file_info in summary:
                context_info += f"- `{file_info['path']}` ({file_info['type']})"
                if file_info['classes']:
                    context_info += f" - 클래스: {', '.join(file_info['classes'])}"
                if file_info['functions']:
                    context_info += f" - 함수: {', '.join(file_info['functions'][:3])}"
                    if len(file_info['functions']) > 3:
                        context_info += f" (+{len(file_info['functions'])-3}개 더)"
                context_info += "\n"

            full_prompt = f"{prompt_text}\n\n{context_info}\n\n위 파일들은 서로 연관된 파일 그룹입니다. 종합적으로 검토해주세요."

            try:
                review = review_with_gemini_cli(combined_diff, full_prompt)
                comment = f"<!-- REVIEWED_COMMIT:{latest_commit_sha} -->\n\n### 🤖 Gemini 복합 코드리뷰: {group_type.upper()} (커밋: {latest_commit_sha[:8]})\n\n{context_info}\n\n{review}"
                post_mr_comment(comment)
                print(f"✅ {main_file} 그룹 리뷰 완료")

            except Exception as e:
                print(f"❌ {main_file} 그룹 리뷰 실패: {e}")
                continue

        print("🎉 모든 복합 파일 그룹 리뷰가 완료되었습니다!")

    except KeyboardInterrupt:
        print("\n⏹️ 사용자에 의해 중단되었습니다.")
        sys.exit(1)
    except Exception as e:
        print(f"💥 예상치 못한 오류 발생: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
