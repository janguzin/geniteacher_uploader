# uploader.py
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from pathlib import Path
import csv, re, os

# ===== 페이지/셀렉터(네 화면에 맞게 확인) =====
UPLOAD_URL   = "https://www.geniteacher.com/test-paper-upsert?id=0"   # 문제 생성 첫 화면 URL
SEL_TITLE    = 'input[placeholder="문제 등록을 위해 먼저 학습지명을 작성해 주세요."]'
TEXT_NEXT    = "다음"
TEXT_SAVE    = "저장"
TEXT_BTN_PROBLEM  = "문제지 파일 선택"
TEXT_BTN_SOLUTION = "답안지 파일 선택"
SEL_OCR_DONE = None  # OCR 완료 문구가 화면에 있으면 예: 'text=OCR 완료' 로 변경
# ============================================

CSV_PATH       = "jobs.csv"
AUTH_STATE     = "auth_state.json"
TIMEOUT_UI_MS  = 30_000
TIMEOUT_OCR_MS = 15 * 60 * 1000  # 15분

def read_jobs():
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            cats = (r.get("categories") or "").strip()
            cats_list = [c.strip() for c in re.split(r"[|,]", cats) if c.strip()]
            yield dict(
                name=r["name"].strip(),
                problem=Path(r["problem_path"]).as_posix(),
                solution=Path(r["solution_path"]).as_posix(),
                categories=cats_list
            )

def wait_button_enabled(page, text, timeout=30_000):
    page.wait_for_function(
        """
        (t) => {
          const all = [...document.querySelectorAll('button, [role=button]')];
          const btn = all.find(el => (el.innerText||el.textContent||'').includes(t));
          return btn && !btn.disabled && !btn.ariaDisabled;
        }
        """,
        arg=text, timeout=timeout
    )

def click_button_by_text(page, text, timeout=30_000):
    wait_button_enabled(page, text, timeout=timeout)
    # role=button 우선, 실패 시 텍스트로
    try:
        page.get_by_role("button", name=text).click()
    except Exception:
        page.get_by_text(text, exact=True).click()

def solidify_input(page, selector):
    page.dispatch_event(selector, "input")
    page.dispatch_event(selector, "change")
    try:
        page.press(selector, "Enter")
    except Exception:
        pass
    page.click("body")

def select_categories(page, names):
    if not names:
        return
    cat_input = None
    # 1) placeholder 시도
    try:
        cat_input = page.get_by_placeholder("카테고리")
        cat_input.wait_for(timeout=1500)
    except Exception:
        cat_input = None
    # 2) '카테고리' 라벨 인근 input
    if cat_input is None:
        try:
            container = page.locator("section,div,form").filter(has_text="카테고리").first
            cat_input = container.locator("input").first
            cat_input.wait_for(timeout=1500)
        except Exception:
            cat_input = None
    # 3) combobox 역할 input
    if cat_input is None:
        try:
            cat_input = page.locator('input[role="combobox"]').first
            cat_input.wait_for(timeout=1500)
        except Exception:
            pass
    if cat_input is None:
        print("[INFO] 카테고리 입력칸을 못 찾았지만, 카테고리 없이 진행합니다.")
        return

    for name in names:
        try:
            cat_input.fill("")
            cat_input.type(name, delay=50)
            page.wait_for_timeout(600)
            clicked = False
            try:
                page.locator("li,div[role='option']").filter(has_text=name).first.click(timeout=800)
                clicked = True
            except Exception:
                pass
            if not clicked:
                cat_input.press("Enter")
            page.wait_for_timeout(150)
        except Exception as e:
            print(f"[WARN] 카테고리 추가 실패: {name} / {e}")

def assert_file(p: str, label: str):
    path = Path(p)
    if not path.exists():
        raise FileNotFoundError(f"{label} 경로가 존재하지 않습니다: {p}")
    if path.is_dir():
        raise FileNotFoundError(f"{label} 경로가 파일이 아니라 폴더입니다: {p}")
    return path.as_posix()

def _wait_filename_near_button(page, button_name: str, filename: str, timeout=5000):
    """
    업로드 버튼 근처에 파일명이 노출되는 UI가 흔해 이를 근거로 업로드 반영을 검증.
    파일명 일부만 뜨는 경우를 고려해 basename으로 부분 매칭.
    """
    base = os.path.basename(filename)
    try:
        btn = page.get_by_role("button", name=button_name)
    except Exception:
        btn = page.get_by_text(button_name, exact=True)
    # 버튼의 조상 컨테이너 근처에서 파일명 텍스트 대기
    container = btn.locator("xpath=ancestor-or-self::*[self::button or @role='button' or self::div or self::section][1]")
    page.wait_for_function(
        """([sel, base]) => {
            const root = document.querySelector(sel);
            if (!root) return false;
            const text = (root.innerText||root.textContent||'') + ' ' + (root.parentElement?.innerText||'');
            return text.includes(base);
        }""",
        arg=[container.evaluate("e=>e.tagName.toLowerCase()==='button'?'button':null") or "button", base],
        timeout=timeout
    )

def upload_one_with_file_chooser(page, button_name: str, filepath: str, timeout=TIMEOUT_UI_MS):
    """
    파일선택 다이얼로그를 수신하는 가장 안전한 방식.
    """
    try:
        with page.expect_file_chooser(timeout=timeout) as fc_info:
            # 버튼 클릭으로 파일선택 유도 (role 실패 시 텍스트로 시도)
            try:
                page.get_by_role("button", name=button_name).click()
            except Exception:
                page.get_by_text(button_name, exact=True).click()
        fc = fc_info.value
        fc.set_files(filepath)
        # 파일명 표시 대기(가능한 경우)
        try:
            _wait_filename_near_button(page, button_name, filepath, timeout=5000)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[WARN] 파일선택기 방식 실패({button_name}): {e}")
        return False

def upload_one_fallback_inject(page, button_name: str, filepath: str, timeout=TIMEOUT_UI_MS):
    """
    버튼 주변에서 가장 가까운 input[type=file]을 찾아 직접 주입하는 정밀 대안.
    버튼 클릭 후 DOM 변화를 잠시 기다리고 인접 input을 탐색.
    """
    # 버튼 한 번 눌러서 렌더를 유도
    try:
        try:
            btn = page.get_by_role("button", name=button_name)
        except Exception:
            btn = page.get_by_text(button_name, exact=True)
        btn.click(timeout=2000)
    except Exception:
        pass

    # 버튼 기준 근접 input[type=file] 탐색
    try:
        try:
            btn = page.get_by_role("button", name=button_name)
        except Exception:
            btn = page.get_by_text(button_name, exact=True)

        # 버튼의 조상 컨테이너들에서 파일 인풋 찾기
        input_loc = (
            btn.locator("xpath=ancestor-or-self::*").locator("input[type='file']")
        )
        if input_loc.count() == 0:
            # 전역에서라도 최근에 생성된 보이는 파일 인풋 우선
            input_loc = page.locator("input[type='file']:not([disabled])")

        input_loc.first.set_input_files(filepath)
        # onChange가 먹도록 이벤트 대기
        page.wait_for_timeout(300)

        # 파일명 표시 대기(가능한 경우)
        try:
            _wait_filename_near_button(page, button_name, filepath, timeout=5000)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[ERROR] fallback 주입 실패({button_name}): {e}")
        return False

def upload_files(page, problem_path: str, solution_path: str):
    """
    버튼별로 안전하게 업로드.
    1순위: file chooser, 2순위: 버튼 인접 input 직접 주입
    """
    # 문제지
    ok1 = upload_one_with_file_chooser(page, TEXT_BTN_PROBLEM, problem_path)
    if not ok1:
        ok1 = upload_one_fallback_inject(page, TEXT_BTN_PROBLEM, problem_path)
    if not ok1:
        raise RuntimeError("문제지 업로드 실패")

    # 답안지
    ok2 = upload_one_with_file_chooser(page, TEXT_BTN_SOLUTION, solution_path)
    if not ok2:
        ok2 = upload_one_fallback_inject(page, TEXT_BTN_SOLUTION, solution_path)
    if not ok2:
        raise RuntimeError("답안지 업로드 실패")

def main():
    with sync_playwright() as p:
        # Edge 채널이 없으면 기본 chromium 사용
        try:
            browser = p.chromium.launch(headless=False, channel="msedge")
        except Exception:
            browser = p.chromium.launch(headless=False)

        ctx = browser.new_context(storage_state=AUTH_STATE if Path(AUTH_STATE).exists() else None)
        page = ctx.new_page()

        # 첫 실행: 로그인 세션 저장
        if not Path(AUTH_STATE).exists():
            page.goto(UPLOAD_URL)
            input("[처음 실행] 브라우저에서 로그인 완료 후 콘솔에 Enter → ")
            ctx.storage_state(path=AUTH_STATE)

        for job in read_jobs():
            print(f"\n[RUN] {job['name']}")
            try:
                # 1) 첫 화면 로드
                page.goto(UPLOAD_URL, wait_until="domcontentloaded")

                # 2) 학습지명 입력
                page.wait_for_selector(SEL_TITLE, timeout=TIMEOUT_UI_MS)
                page.fill(SEL_TITLE, job["name"])
                solidify_input(page, SEL_TITLE)

                # 3) 카테고리 (있으면)
                select_categories(page, job["categories"])

                # 4) 다음
                click_button_by_text(page, TEXT_NEXT, timeout=30_000)

                # 5) 업로드 화면 준비 (버튼 보일 때까지)
                try:
                    page.get_by_role("button", name=TEXT_BTN_PROBLEM).wait_for(timeout=TIMEOUT_UI_MS)
                except Exception:
                    page.get_by_text(TEXT_BTN_PROBLEM, exact=True).wait_for(timeout=TIMEOUT_UI_MS)

                try:
                    page.get_by_role("button", name=TEXT_BTN_SOLUTION).wait_for(timeout=TIMEOUT_UI_MS)
                except Exception:
                    page.get_by_text(TEXT_BTN_SOLUTION, exact=True).wait_for(timeout=TIMEOUT_UI_MS)

                # 6) 파일 존재 확인 후 업로드
                prob = assert_file(job["problem"], "문제지")
                sol  = assert_file(job["solution"], "답안지")
                upload_files(page, prob, sol)

                # 7) 다음 → OCR 대기
                click_button_by_text(page, TEXT_NEXT, timeout=30_000)

                # 8) OCR 완료 → 저장
                if SEL_OCR_DONE:
                    try:
                        page.wait_for_selector(SEL_OCR_DONE, timeout=TIMEOUT_OCR_MS)
                    except PWTimeout:
                        print("[INFO] OCR 문구 미검출 → '저장' 버튼 활성화로 대기")
                click_button_by_text(page, TEXT_SAVE, timeout=TIMEOUT_OCR_MS)

                print(f"[OK] {job['name']} 완료")

            except Exception as e:
                safe = re.sub(r'[^\w가-힣.-]+','_', job['name'])
                shot = f"fail_{safe}.png"
                try:
                    page.screenshot(path=shot, full_page=True)
                except Exception:
                    pass
                print(f"[FAIL] {job['name']} / {e} / 스샷: {shot}")

        ctx.storage_state(path=AUTH_STATE)
        browser.close()

if __name__ == "__main__":
    main()
