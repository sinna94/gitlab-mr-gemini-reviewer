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


def get_mr_changes_with_commits():
    """MR 전체 변경사항과 커밋 정보를 함께 가져오기"""
    # MR 정보 가져오기
    mr_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}"
    headers = {"PRIVATE-TOKEN": gitlab_token}

    mr_resp = requests.get(mr_url, headers=headers)
    mr_resp.raise_for_status()
    mr_data = mr_resp.json()

    # MR의 모든 커밋 가져오기
    commits_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}/commits"
    commits_resp = requests.get(commits_url, headers=headers)
    commits_resp.raise_for_status()
    commits = commits_resp.json()

    # MR 전체 변경사항 가져오기
    changes_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}/changes"
    changes_resp = requests.get(changes_url, headers=headers)
    changes_resp.raise_for_status()
    changes = changes_resp.json()['changes']

    return {
        'changes': changes,
        'commits': commits,
        'mr_info': mr_data,
        'latest_commit_sha': commits[0]['id'] if commits else None
    }

def get_reviewed_commits():
    """이미 리뷰된 커밋들 목록 가져오기"""
    notes_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}/notes"
    headers = {"PRIVATE-TOKEN": gitlab_token}

    try:
        resp = requests.get(notes_url, headers=headers)
        resp.raise_for_status()
        notes = resp.json()

        reviewed_commits = set()
        for note in notes:
            body = note.get('body', '')
            # 리뷰 마커에서 커밋 SHA 추출
            import re
            matches = re.findall(r'<!-- REVIEWED_COMMIT:([a-f0-9]+) -->', body)
            reviewed_commits.update(matches)

        return reviewed_commits
    except Exception as e:
        print(f"기존 리뷰 확인 중 오류: {e}")
        return set()

def filter_new_changes(changes, commits, reviewed_commits):
    """새로운 변경사항만 필터링"""
    if not reviewed_commits:
        return changes, commits

    # 새로운 커밋들 찾기
    new_commits = [commit for commit in commits if commit['id'] not in reviewed_commits]

    if not new_commits:
        return [], []

    # 새로운 커밋들의 변경사항만 추출
    new_changes = []
    new_commit_shas = {commit['id'] for commit in new_commits}

    # 각 새로운 커밋의 diff 가져오기
    headers = {"PRIVATE-TOKEN": gitlab_token}

    for commit in new_commits:
        try:
            commit_diff_url = f"{gitlab_api_url}/projects/{gitlab_project_id}/repository/commits/{commit['id']}/diff"
            diff_resp = requests.get(commit_diff_url, headers=headers)
            diff_resp.raise_for_status()
            commit_changes = diff_resp.json()

            # 커밋 정보 추가
            for change in commit_changes:
                change['commit_sha'] = commit['id']
                change['commit_message'] = commit['message']
                change['commit_author'] = commit['author_name']

            new_changes.extend(commit_changes)
        except Exception as e:
            print(f"커밋 {commit['id'][:8]} diff 가져오기 실패: {e}")
            continue

    return new_changes, new_commits

def group_changes_by_commit(changes):
    """커밋별로 변경사항 그룹핑"""
    commit_groups = {}

    for change in changes:
        commit_sha = change.get('commit_sha', 'unknown')
        if commit_sha not in commit_groups:
            commit_groups[commit_sha] = {
                'changes': [],
                'commit_info': {
                    'sha': commit_sha,
                    'message': change.get('commit_message', ''),
                    'author': change.get('commit_author', '')
                }
            }
        commit_groups[commit_sha]['changes'].append(change)

    return commit_groups

def should_review_incrementally(mr_info, commits):
    """점진적 리뷰를 할지 전체 리뷰를 할지 결정"""
    # MR이 새로 생성되었거나 첫 리뷰인 경우 전체 리뷰
    if mr_info.get('state') == 'opened' and len(commits) <= 3:
        return False

    # 커밋이 많은 경우 점진적 리뷰
    if len(commits) > 5:
        return True

    # 기본적으로 점진적 리뷰
    return True


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

        # MR 정보와 변경사항 가져오기
        try:
            mr_data = get_mr_changes_with_commits()
            all_changes = mr_data['changes']
            all_commits = mr_data['commits']
            mr_info = mr_data['mr_info']
            latest_commit_sha = mr_data['latest_commit_sha']
        except requests.exceptions.RequestException as e:
            print(f"GitLab API 호출 오류: {e}")
            sys.exit(1)

        if not all_changes:
            print("리뷰할 변경사항이 없습니다.")
            return

        # 이미 리뷰된 커밋들 확인
        reviewed_commits = get_reviewed_commits()

        # 점진적 리뷰 여부 결정
        incremental_review = should_review_incrementally(mr_info, all_commits)

        if incremental_review and reviewed_commits:
            print(f"점진적 리뷰 모드: 이미 리뷰된 커밋 {len(reviewed_commits)}개 제외")
            changes, new_commits = filter_new_changes(all_changes, all_commits, reviewed_commits)

            if not changes:
                print("새로운 변경사항이 없습니다. 모든 커밋이 이미 리뷰되었습니다.")
                return

            print(f"새로운 커밋 {len(new_commits)}개에 대해 리뷰를 진행합니다.")

            # 커밋별로 리뷰 진행
            commit_groups = group_changes_by_commit(changes)

            for commit_sha, commit_group in commit_groups.items():
                if commit_sha == 'unknown':
                    continue

                commit_changes = commit_group['changes']
                commit_info = commit_group['commit_info']

                print(f"\n📝 커밋 {commit_sha[:8]} 리뷰 시작")
                print(f"   메시지: {commit_info['message'][:50]}...")
                print(f"   작성자: {commit_info['author']}")

                # 파일 그룹핑
                file_groups = advanced_group_related_files(commit_changes)

                for i, group in enumerate(file_groups, 1):
                    group_type = group['type']
                    main_file = group['main_file']
                    files = group['files']
                    summary = group['summary']

                    print(f"   🔍 [{i}/{len(file_groups)}] 리뷰 중: {main_file} ({group_type})")

                    # diff 결합
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

                    # 커밋 컨텍스트 추가
                    context_info = f"""
📋 **커밋 정보**:
- 커밋 SHA: {commit_sha[:8]}
- 커밋 메시지: {commit_info['message']}
- 작성자: {commit_info['author']}

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
                        success = post_combined_review(
                            combined_diff,
                            context_info,
                            prompt_text,
                            commit_sha,
                            group_type,
                            main_file
                        )
                        if success:
                            print(f"   ✅ {main_file} 그룹 리뷰 완료")
                        else:
                            print(f"   ❌ {main_file} 그룹 리뷰 실패")

                    except Exception as e:
                        print(f"   ❌ {main_file} 그룹 리뷰 실패: {e}")
                        continue

        else:
            print("전체 리뷰 모드: MR의 모든 변경사항을 리뷰합니다.")

            # 전체 파일 그룹핑
            file_groups = advanced_group_related_files(all_changes)

            print(f"📝 MR {gitlab_mr_iid}의 {len(file_groups)}개 복합 그룹에 대한 전체 리뷰를 시작합니다...")

            for i, group in enumerate(file_groups, 1):
                group_type = group['type']
                main_file = group['main_file']
                files = group['files']
                summary = group['summary']

                print(f"🔍 [{i}/{len(file_groups)}] 리뷰 중: {main_file} ({group_type})")

                # diff 결합
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

                # 전체 리뷰 컨텍스트
                context_info = f"""
📋 **MR 전체 리뷰**:
- MR 번호: {gitlab_mr_iid}
- 전체 커밋 수: {len(all_commits)}
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

                try:
                    success = post_combined_review(
                        combined_diff,
                        context_info,
                        prompt_text,
                        latest_commit_sha,
                        group_type,
                        main_file
                    )
                    if success:
                        print(f"✅ {main_file} 그룹 리뷰 완료")
                    else:
                        print(f"❌ {main_file} 그룹 리뷰 실패")

                except Exception as e:
                    print(f"❌ {main_file} 그룹 리뷰 실패: {e}")
                    continue

        print("🎉 모든 코드 리뷰가 완료되었습니다!")

    except KeyboardInterrupt:
        print("\n⏹️ 사용자에 의해 중단되었습니다.")
        sys.exit(1)
    except Exception as e:
        print(f"💥 예상치 못한 오류 발생: {e}")
        sys.exit(1)


def post_combined_review(combined_diff, context_info, prompt_text, commit_sha, group_type, main_file):
    """Gemini CLI로 리뷰를 생성하고 인라인 댓글로 작성"""
    try:
        # Gemini로 리뷰 생성
        full_prompt = f"{prompt_text}\n\n{context_info}\n\n위 파일들에 대해 구체적인 개선사항을 파일명과 라인번호를 포함하여 제안해주세요. 형식: 파일명:라인번호 - 개선사항"
        review = review_with_gemini_cli(combined_diff, full_prompt)

        # Gemini 리뷰에서 파일별 라인별 댓글 추출
        inline_suggestions = parse_gemini_review_for_inline_comments(review, combined_diff)

        # 인라인 댓글 작성
        inline_count = 0

        for suggestion in inline_suggestions:
            try:
                post_inline_comment(
                    suggestion['file'],
                    suggestion['line'],
                    suggestion['message'],
                    commit_sha
                )
                inline_count += 1
            except Exception as e:
                print(f"인라인 댓글 작성 실패: {e}")
                continue

        # 리뷰 완료 마커만 추가 (숨김 댓글)
        marker_comment = f"<!-- REVIEWED_COMMIT:{commit_sha} -->"
        post_mr_comment(marker_comment)

        if inline_count > 0:
            print(f"   📍 {inline_count}개의 인라인 댓글 추가 (Gemini)")
        else:
            print(f"   ✅ 특별한 개선사항 없음")

        return True

    except Exception as e:
        print(f"리뷰 작성 실패: {e}")
        return False


def parse_gemini_review_for_inline_comments(review, combined_diff):
    """Gemini 리뷰에서 파일별 라인별 댓글을 추출하여 인라인 댓글용 데이터로 변환"""
    suggestions = []

    # diff에서 파일별 라인 정보 미리 추출
    line_info = parse_diff_for_line_info(combined_diff)
    file_lines = {}
    for info in line_info:
        if info['file'] not in file_lines:
            file_lines[info['file']] = []
        file_lines[info['file']].append(info)

    lines = review.split('\n')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 파일명:라인번호 형태 패턴 찾기
        file_line_pattern = r'([^:]+):(\d+)\s*[-–]\s*(.+)'
        match = re.match(file_line_pattern, line)

        if match:
            file_path = match.group(1).strip()
            try:
                line_number = int(match.group(2))
                message = match.group(3).strip()

                # 파일 경로 정규화 (diff에서 추출한 파일명과 매칭)
                normalized_file = normalize_file_path(file_path, file_lines.keys())

                if normalized_file and message:
                    suggestions.append({
                        'file': normalized_file,
                        'line': line_number,
                        'message': f"🤖 **Gemini 제안**: {message}"
                    })
            except ValueError:
                continue

        # 다른 패턴들도 시도
        # "파일명의 라인 X에서..." 형태
        alt_pattern = r'([^의]+)의?\s*라인\s*(\d+)에서?\s*[:-]?\s*(.+)'
        alt_match = re.search(alt_pattern, line)

        if alt_match:
            file_path = alt_match.group(1).strip()
            try:
                line_number = int(alt_match.group(2))
                message = alt_match.group(3).strip()

                normalized_file = normalize_file_path(file_path, file_lines.keys())

                if normalized_file and message:
                    suggestions.append({
                        'file': normalized_file,
                        'line': line_number,
                        'message': f"🤖 **Gemini 제안**: {message}"
                    })
            except ValueError:
                continue

    # 중복 제거
    unique_suggestions = []
    seen = set()
    for suggestion in suggestions:
        key = (suggestion['file'], suggestion['line'])
        if key not in seen:
            seen.add(key)
            unique_suggestions.append(suggestion)

    return unique_suggestions


def normalize_file_path(gemini_file_path, actual_file_paths):
    """Gemini가 언급한 파일 경로를 실제 diff의 파일 경로와 매칭"""
    # 정확히 일치하는 경우
    if gemini_file_path in actual_file_paths:
        return gemini_file_path

    # 파일명만 비교
    gemini_basename = os.path.basename(gemini_file_path)
    for actual_path in actual_file_paths:
        if os.path.basename(actual_path) == gemini_basename:
            return actual_path

    # 부분 매칭
    for actual_path in actual_file_paths:
        if gemini_file_path in actual_path or actual_path in gemini_file_path:
            return actual_path

    return None


def generate_smart_gemini_prompt(combined_diff, context_info):
    """더 구체적인 인라인 댓글을 위한 Gemini 프롬프트 생성"""
    return f"""다음 코드 변경사항을 리뷰하고, 구체적인 개선사항을 제안해주세요.

{context_info}

응답 형식을 다음과 같이 해주세요:
- 각 개선사항은 "파일명:라인번호 - 개선사항 설명" 형태로 작성
- 구체적이고 실행 가능한 제안만 포함
- 코드 품질, 성능, 보안, 가독성 관점에서 검토
- 불필요한 서론이나 결론 없이 개선사항만 나열

예시:
src/main.py:15 - 변수명 'data'를 더 구체적인 이름으로 변경하세요
utils/helper.js:23 - 이 함수는 너무 길어서 여러 함수로 분리하는 것이 좋겠습니다

코드 변경사항:
{combined_diff}"""


def post_inline_comment(file_path, line_number, comment_text, commit_sha):
    """특정 파일의 라인에 인라인 댓글을 달기"""
    url = f"{gitlab_api_url}/projects/{gitlab_project_id}/merge_requests/{gitlab_mr_iid}/notes"
    headers = {"PRIVATE-TOKEN": gitlab_token}

    # GitLab API의 position 파라미터 구성
    position = {
        "base_sha": commit_sha,
        "start_sha": commit_sha,
        "head_sha": commit_sha,
        "old_path": file_path,
        "new_path": file_path,
        "position_type": "text",
        "new_line": line_number
    }

    data = {
        "body": comment_text,
        "position": json.dumps(position)
    }

    try:
        resp = requests.post(url, headers=headers, data=data)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        print(f"인라인 댓글 생성 실패 (파일: {file_path}, 라인: {line_number}): {e}")
        # 인라인 댓글 실패 시 일반 댓글로 대체
        fallback_comment = f"**파일: `{file_path}` (라인 {line_number})**\n\n{comment_text}"
        return post_mr_comment(fallback_comment)


def parse_diff_for_line_info(diff_content):
    """diff 내용을 파싱하여 변경된 라인 정보를 추출"""
    lines_info = []
    current_file = None
    new_line_num = 0

    for line in diff_content.split('\n'):
        if line.startswith('diff --git'):
            # 파일명 추출
            parts = line.split(' ')
            if len(parts) >= 4:
                current_file = parts[3][2:]  # "b/" 제거
        elif line.startswith('@@'):
            # 라인 번호 정보 추출 (예: @@ -1,4 +1,6 @@)
            match = re.search(r'\+(\d+)', line)
            if match:
                new_line_num = int(match.group(1)) - 1
        elif line.startswith('+') and not line.startswith('+++'):
            # 추가된 라인
            new_line_num += 1
            if current_file:
                lines_info.append({
                    'file': current_file,
                    'line': new_line_num,
                    'type': 'added',
                    'content': line[1:]  # '+' 제거
                })
        elif line.startswith('-') and not line.startswith('---'):
            # 삭제된 라인 (라인 번호는 증가하지 않음)
            if current_file:
                lines_info.append({
                    'file': current_file,
                    'line': new_line_num,
                    'type': 'removed',
                    'content': line[1:]  # '-' 제거
                })
        elif not line.startswith('\\'):
            # 변경되지 않은 라인
            new_line_num += 1

    return lines_info


if __name__ == "__main__":
    main()
