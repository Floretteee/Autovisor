# encoding=utf-8
import asyncio
import os
import time
import traceback
import sys
from dataclasses import dataclass
from typing import Optional
from playwright.async_api import async_playwright, Playwright, Page, BrowserContext, Browser
from playwright.async_api import TimeoutError
from playwright._impl._errors import TargetClosedError
from modules.logger import Logger
from modules.configs import Config
from modules.progress import get_course_progress, show_course_progress
from modules.utils import optimize_page, get_lesson_name, get_filtered_class, get_video_attr, hide_window, \
     save_cookies, load_cookies, clear_cookies, get_runtime_path
from modules.slider import slider_verify
from modules.tasks import video_optimize, play_video, skip_questions, wait_for_verify, task_monitor
from modules import installer
from modules.banner import print_banner

COOKIE_PATH = get_runtime_path("res", "cookies.json")
COOKIE_SAVE_LOCK = asyncio.Lock()


@dataclass
class LearningJob:
    course_url: str
    file_id: Optional[str]
    title: str
    kind: str
    is_new_version: bool
    is_hike_class: bool


class OverallProgress:
    def __init__(self, total: int):
        self.total = max(total, 1)
        self.done = 0
        self.active = {}
        self.lock = asyncio.Lock()

    async def start(self, worker_id: int, title: str) -> None:
        async with self.lock:
            self.active[worker_id] = title
            self.render()

    async def finish(self, worker_id: int) -> None:
        async with self.lock:
            self.done += 1
            self.active.pop(worker_id, None)
            self.render()

    async def skip(self, worker_id: int) -> None:
        await self.finish(worker_id)

    def render(self) -> None:
        percent = int(self.done / self.total * 100)
        width = 30
        filled = int(percent * width // 100)
        bar = ("#" * filled).ljust(width, "-")
        active_workers = ",".join(str(worker_id) for worker_id in sorted(self.active)) or "-"
        text = f"\r总体进度 |{bar}| {percent:3d}%  {self.done}/{self.total}  运行窗口:{active_workers}"
        print(text.ljust(100), end="", flush=True)


async def wait_for_interruption(event_loop: asyncio.Event) -> float:
    event_loop.clear()
    wait_start = time.time()
    await event_loop.wait()
    return time.time() - wait_start


def cal_time_period(start_time: float, paused_time: float) -> float:
    return max(0.0, time.time() - start_time - paused_time)

async def init_page(p: Playwright, cookies, worker_id: int = 1) -> tuple[Page, BrowserContext, Browser]:
    driver = "msedge" if config.driver == "edge" else config.driver
    logger.info(f"Worker-{worker_id} 正在启动{config.driver}浏览器...")
    window_x = 80 + (worker_id - 1) * 40
    window_y = 60 + (worker_id - 1) * 40
    launch_args = {
        "channel": driver,
        "headless": False,
        "executable_path": config.exe_path if config.exe_path else None,
        "args": [
            f'--window-size={1600},{900}',
            f'--window-position={window_x},{window_y}',
        ],
    }
    try:
        browser = await p.chromium.launch(**launch_args)
    except TargetClosedError as e:
        logger.log_exception("首次启动浏览器失败,准备重试.", e)
        logger.info(f"Worker-{worker_id} 检测到浏览器首次启动失败,正在重试...")
        await asyncio.sleep(1)
        browser = await p.chromium.launch(**launch_args)
    context = await browser.new_context()
    # 加载 Cookies
    if cookies:
        await context.add_cookies(cookies)
        logger.info("已加载 Cookies!")
    else:
        logger.info("未找到 Cookies,将跳转至登录页.")
    page = await context.new_page()
    logger.debug(f"Worker-{worker_id} {config.driver}浏览器启动完成.")
    #抹去特征
    with open('res/stealth.min.js', 'r') as f:
        js = f.read()
    await page.add_init_script(js)
    logger.debug("stealth.js执行完成.")
    page.set_default_timeout(24 * 3600 * 1000)

    return page, context, browser

async def auto_login(context: BrowserContext, page: Page, modules=None):
    cookie_saved = False

    async def request_handler(request):
        nonlocal cookie_saved
        if cookie_saved:
            return
        if "https://www.zhihuishu.com" in request.url:
            cookies = await context.cookies()
            async with COOKIE_SAVE_LOCK:
                save_cookies(cookies, COOKIE_PATH)
            logger.info(f"已保存登录凭证到: {COOKIE_PATH},下次可免密登录.")
            cookie_saved = True

    await page.goto(config.login_url, wait_until="commit")
    if "login" not in page.url:
        logger.info("检测到已登录,跳过登录步骤.")
        return
    await page.wait_for_selector(".wall-main", state='attached')  # 等待登陆界面加载
    page.on('request', request_handler)
    if config.username and config.password:
        await page.wait_for_selector("#lUsername", state="attached")
        await page.wait_for_selector("#lPassword", state="attached")
        await page.locator('#lUsername').fill(config.username)
        await page.locator('#lPassword').fill(config.password)
        await page.wait_for_selector(".wall-sub-btn", state="attached")
        await page.wait_for_timeout(500)
        await page.locator(".wall-sub-btn").first.click()
    if config.enableAutoCaptcha and modules:
        await slider_verify(page, modules)
    await page.wait_for_selector(".wall-main", state='hidden')


async def ensure_login(context: BrowserContext, page: Page, cookies, modules=None):
    if cookies:
        logger.info("正在校验 Cookies 登录状态...")
        await page.goto(config.login_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        if "login" not in page.url:
            logger.info("使用Cookies登录成功!")
            return True
        logger.warn("检测到 Cookies 已失效, 将重新登录.", shift=True)
        clear_cookies(COOKIE_PATH)
        cookies = None

    if not config.username or not config.password:
        logger.info("请手动填写账号密码...")
    logger.info("正在等待登录完成...")
    await auto_login(context, page, modules)
    logger.info("登录成功!")
    return False


async def learning_loop(page: Page, start_time, verify_event: asyncio.Event, answer_event: asyncio.Event,
                        is_new_version=False, is_hike_class=False, print_progress=True):
    paused_time = 0.0
    try:
        cur_time = await get_course_progress(page, is_new_version, is_hike_class)
    except TargetClosedError:
        return paused_time
    while cur_time != "100%":
        try:
            limit_time = config.limitMaxTime
            time_period = cal_time_period(start_time, paused_time) / 60
            if 0 < limit_time <= time_period:
                break
            cur_time = await get_course_progress(page, is_new_version, is_hike_class)
            if print_progress:
                show_course_progress(desc="完成进度:", cur_time=cur_time)
            await asyncio.sleep(0.5)
        except TargetClosedError:
            return paused_time
        except TimeoutError as e:
            if await page.query_selector(".yidun_modal__title"):
                paused_time += await wait_for_interruption(verify_event)
            elif await page.query_selector(".topic-title"):
                paused_time += await wait_for_interruption(answer_event)
            else:
                logger.debug(f"学习进度轮询未命中: {logger.summarize_exception(e)}")
    return paused_time


async def review_loop(page: Page, start_time, verify_event: asyncio.Event, answer_event: asyncio.Event,
                      is_hike_class=False):
    paused_time = 0.0
    total_time = await get_video_attr(page, "duration")
    if total_time is None:
        return paused_time
    try:
        await page.evaluate(config.reset_curtime)  # 重置视频播放时间
    except TargetClosedError:
        return paused_time
    while True:
        try:
            limit_time = config.limitMaxTime
            cur_time = await get_video_attr(page, "currentTime")
            if cur_time is None or cur_time >= total_time:
                break
            time_period = cal_time_period(start_time, paused_time) / 60
            if 0 < limit_time <= time_period:
                break
            show_course_progress(desc="完成进度:", cur_time=time_period, limit_time=limit_time)
            await asyncio.sleep(0.5)
        except TargetClosedError:
            return paused_time
        except TimeoutError as e:
            if await page.query_selector(".yidun_modal__title"):
                paused_time += await wait_for_interruption(verify_event)
            elif await page.query_selector(".topic-title"):
                paused_time += await wait_for_interruption(answer_event)
            else:
                logger.debug(f"复习进度轮询未命中: {logger.summarize_exception(e)}")
    return paused_time


async def working_loop(page: Page, verify_event: asyncio.Event = None, answer_event: asyncio.Event = None,
                       is_new_version=False, is_hike_class=False):
    verify_event = verify_event or asyncio.Event()
    answer_event = answer_event or asyncio.Event()
    # 获取所有课程元素
    if is_hike_class:
        await page.wait_for_selector(".file-item", state="attached")
    else:
        await page.wait_for_selector(".clearfix.video", state="attached")
    to_learn_class = await get_filtered_class(page, is_new_version, is_hike_class)
    learning = True if len(to_learn_class) > 0 else False
    if learning:
        all_class = to_learn_class
    else:
        all_class = await get_filtered_class(page, is_new_version, is_hike_class, include_all=True)
    start_time = time.time()
    paused_time = 0.0
    cur_index = 0

    while cur_index < len(all_class):
        await all_class[cur_index].click()
        if is_hike_class:
            await page.wait_for_selector(".file-item.active", state="attached")
        else:
            await page.wait_for_selector(".current_play", state="attached")
        await page.wait_for_timeout(1000)
        title = await get_lesson_name(page, is_hike_class)
        logger.info(f"正在学习:{title}")
        page.set_default_timeout(10000)
        # 移除视频暂停功能
        await page.wait_for_selector("video", state="attached")
        await page.evaluate(config.remove_pause)
        if learning:
            paused_time += await learning_loop(page, start_time, verify_event, answer_event, is_new_version, is_hike_class)
        else:
            paused_time += await review_loop(page, start_time, verify_event, answer_event, is_hike_class)
        if is_hike_class is False:
            if "current_play" in await all_class[cur_index].get_attribute('class'):
                cur_index += 1
        else:
            if "active" in await all_class[cur_index].get_attribute('class'):
                cur_index += 1
        reachTimeLimit = await check_time_limit(page, start_time, paused_time, all_class, title, is_hike_class)
        if reachTimeLimit:
            return


async def check_time_limit(page: Page, start_time, paused_time, all_class, title, is_hike_class) -> bool:
    reachTimeLimit = False
    page.set_default_timeout(24 * 3600 * 1000)
    time_period = cal_time_period(start_time, paused_time) / 60
    if 0 < config.limitMaxTime <= time_period:
        logger.info(f"当前课程已达时限:{config.limitMaxTime}min", shift=True)
        logger.info("即将进入下门课程!")
        reachTimeLimit = True
    else:
        class_name = await all_class[-1].get_attribute('class')
        if is_hike_class:
            if "active" in class_name:
                logger.info("已学完本课程全部内容!", shift=True)
                print("==" * 10)
            else:
                logger.info(f"\"{title}\" 已完成!", shift=True)
                logger.info(f"本次课程已学习:{time_period:.1f} min")
        else:
            if "current_play" in class_name:
                logger.info("已学完本课程全部内容!", shift=True)
                print("==" * 10)
            else:
                logger.info(f"\"{title}\" 已完成!", shift=True)
                logger.info(f"本次课程已学习:{time_period:.1f} min")
    return reachTimeLimit


async def wait_for_course_ready(page: Page) -> None:
    await page.wait_for_load_state("domcontentloaded")
    for selector in (".file-item", ".clearfix.video", "video"):
        try:
            await page.wait_for_selector(selector, state="attached", timeout=5000)
            return
        except TimeoutError:
            continue


async def load_course_page(page: Page, context: BrowserContext, course_url: str, modules=None) -> None:
    logger.info("正在加载播放页...")
    await page.goto(course_url, wait_until="commit")
    await page.wait_for_timeout(1500)
    if "login" in page.url:
        logger.warn("播放页跳转到登录页, 当前登录状态已失效, 正在重新登录.", shift=True)
        clear_cookies(COOKIE_PATH)
        await ensure_login(context, page, None, modules)
        logger.info("重新进入播放页...")
        await page.goto(course_url, wait_until="commit")
        await page.wait_for_timeout(1500)
    await wait_for_course_ready(page)


async def set_worker_title(page: Page, worker_id: int) -> str:
    window_title = f"Autovisor-Worker-{worker_id}"
    try:
        await page.evaluate(f'document.title = "{window_title}"')
    except Exception:
        pass
    return window_title


async def scan_learning_jobs(page: Page, course_url: str) -> list[LearningJob]:
    is_new_version = "fusioncourseh5" in course_url
    is_hike_class = "hike.zhihuishu.com" in course_url
    if await page.locator(".file-item").count() > 0:
        raw_jobs = await page.evaluate('''() => Array.from(document.querySelectorAll('.file-item')).map((item, index) => {
            const id = item.id || '';
            const name = item.querySelector('.file-name')?.textContent?.trim()
                || item.querySelector('span')?.textContent?.trim()
                || id
                || `资源${index + 1}`;
            const status = item.querySelector('.status-box')?.textContent?.trim() || '';
            const done = !!item.querySelector('.icon-finish') || status === '100%';
            const rate = item.querySelector('.rate')?.textContent?.trim() || '';
            const kind = item.querySelector('.icon-video') ? 'video'
                : item.querySelector('.icon-doc') ? 'doc'
                : item.querySelector('.icon-work') ? 'work'
                : 'unknown';
            return { id, name, done, rate, kind };
        })''')
        no_progress_jobs = []
        partial_progress_jobs = []
        seen = set()
        for item in raw_jobs:
            file_id = str(item.get("id") or "").strip()
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            kind = str(item.get("kind") or "unknown")
            if kind != "video":
                continue
            rate = str(item.get("rate") or "").strip()
            try:
                rate_num = float(rate.replace("%", "")) if rate else None
            except ValueError:
                rate_num = None
            if item.get("done") or (rate_num is not None and rate_num >= 100):
                continue
            job = LearningJob(
                course_url=course_url,
                file_id=file_id,
                title=str(item.get("name") or file_id),
                kind=kind,
                is_new_version=is_new_version,
                is_hike_class=True,
            )
            if rate_num is None or rate_num <= 0:
                no_progress_jobs.append(job)
            else:
                partial_progress_jobs.append(job)
        logger.info(
            f"待学习视频: 无进度 {len(no_progress_jobs)} 个, 待补进度 {len(partial_progress_jobs)} 个."
        )
        jobs = no_progress_jobs + partial_progress_jobs
        return jobs

    return [LearningJob(
        course_url=course_url,
        file_id=None,
        title=course_url,
        kind="course",
        is_new_version=is_new_version,
        is_hike_class=is_hike_class,
    )]


async def collect_jobs(p: Playwright, cookies, modules=None) -> list[LearningJob]:
    page, context, browser = await init_page(p, cookies, worker_id=0)
    jobs = []
    try:
        await ensure_login(context, page, cookies, modules)
        for course_url in dict.fromkeys(config.course_urls):
            await load_course_page(page, context, course_url, modules)
            is_new_version = "fusioncourseh5" in course_url
            is_hike_class = "hike.zhihuishu.com" in course_url
            await optimize_page(page, config, is_new_version, is_hike_class)
            course_jobs = await scan_learning_jobs(page, course_url)
            jobs.extend(course_jobs)
            logger.info(f"已扫描到 {len(course_jobs)} 个待处理资源.")
    finally:
        try:
            await context.close()
            await browser.close()
        except Exception as e:
            logger.debug(f"扫描浏览器关闭时忽略异常: {logger.summarize_exception(e)}")
    return jobs


async def has_video(page: Page, timeout: int = 6000) -> bool:
    try:
        await page.wait_for_selector("video", state="attached", timeout=timeout)
        return True
    except TimeoutError:
        return False


async def start_current_video(page: Page) -> None:
    await page.evaluate('''() => {
        const video = document.querySelector('video');
        if (!video) return;
        video.muted = true;
        video.pause = () => {};
        const playPromise = video.play();
        if (playPromise && playPromise.catch) playPromise.catch(() => {});
    }''')


async def get_active_title(page: Page, fallback: str) -> str:
    for selector in ("#sourceTit span", ".file-item.active .file-name", "#lessonOrder", ".current_play"):
        try:
            item = await page.query_selector(selector)
            if not item:
                continue
            title = await item.get_attribute("title") or await item.text_content()
            if title and title.strip():
                return title.strip()
        except Exception:
            continue
    return fallback


async def wait_resource_active(page: Page, file_id: str) -> None:
    plain_id = file_id.replace("file_", "")
    try:
        await page.wait_for_function('''targetId => {
            const active = document.querySelector(`#file_${targetId}.active`);
            const hiddenFileId = document.querySelector('#fileId')?.value;
            return !!active || hiddenFileId === targetId;
        }''', arg=plain_id, timeout=6000)
    except TimeoutError:
        logger.debug(f"等待资源切换超时: {file_id}")


async def open_resource(page: Page, file_id: str) -> bool:
    target = await page.query_selector(f"#{file_id}")
    if target:
        await target.click()
        return True
    plain_id = file_id.replace("file_", "")
    return await page.evaluate('''targetId => {
        if (typeof window.changeFile === 'function') {
            window.changeFile(Number(targetId));
            return true;
        }
        const target = document.querySelector(`#file_${targetId}`);
        if (target) {
            target.click();
            return true;
        }
        return false;
    }''', plain_id)


async def run_resource_job(page: Page, job: LearningJob, worker_id: int,
                           verify_event: asyncio.Event, answer_event: asyncio.Event,
                           progress: OverallProgress) -> None:
    if not job.file_id:
        await working_loop(page, verify_event, answer_event, job.is_new_version, job.is_hike_class)
        await progress.finish(worker_id)
        return
    opened = await open_resource(page, job.file_id)
    if not opened:
        logger.warn(f"Worker-{worker_id} 无法打开资源 {job.file_id},跳过.", shift=True)
        await progress.skip(worker_id)
        return
    await wait_resource_active(page, job.file_id)
    await page.wait_for_timeout(800)
    title = await get_active_title(page, job.title)
    if job.kind != "video":
        logger.info(f"Worker-{worker_id} 跳过非视频资源:{title}", shift=True)
        await progress.skip(worker_id)
        return
    if not await has_video(page):
        logger.info(f"Worker-{worker_id} 当前资源未加载视频,自动跳过:{title}", shift=True)
        await progress.skip(worker_id)
        return
    logger.info(f"Worker-{worker_id} 正在学习:{title}", shift=True)
    await progress.start(worker_id, title)
    page.set_default_timeout(10000)
    await start_current_video(page)
    start_time = time.time()
    await learning_loop(page, start_time, verify_event, answer_event, job.is_new_version, True, print_progress=False)
    logger.info(f"Worker-{worker_id} 完成资源:{title}", shift=True)
    await progress.finish(worker_id)


async def worker_loop(worker_id: int, p: Playwright, job_queue: asyncio.Queue, cookies, progress: OverallProgress,
                      modules=None) -> None:
    page, context, browser = await init_page(p, cookies, worker_id=worker_id)
    tasks = []
    current_course_url = None
    window_title = f"Autovisor-Worker-{worker_id}"
    verify_event = asyncio.Event()
    answer_event = asyncio.Event()
    try:
        await ensure_login(context, page, cookies, modules)
        verify_task = asyncio.create_task(wait_for_verify(page, config, verify_event, window_title))
        video_optimize_task = asyncio.create_task(video_optimize(page, config))
        skip_ques_task = asyncio.create_task(skip_questions(page, answer_event))
        play_video_task = asyncio.create_task(play_video(page))
        tasks.extend([verify_task, video_optimize_task, skip_ques_task, play_video_task])
        if config.enableHideWindow:
            await hide_window(page, window_title)

        while True:
            try:
                job = job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if current_course_url != job.course_url:
                    await load_course_page(page, context, job.course_url, modules)
                    await optimize_page(page, config, job.is_new_version, "hike.zhihuishu.com" in job.course_url)
                    await set_worker_title(page, worker_id)
                    current_course_url = job.course_url
                await run_resource_job(page, job, worker_id, verify_event, answer_event, progress)
            except TargetClosedError:
                logger.warn(f"Worker-{worker_id} 浏览器已关闭,停止该窗口任务.", shift=True)
                break
            except Exception as e:
                logger.log_exception(f"Worker-{worker_id} 处理资源失败:{job.title}", e, shift=True)
                await progress.skip(worker_id)
            finally:
                job_queue.task_done()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await context.close()
            await browser.close()
        except Exception as e:
            logger.debug(f"Worker-{worker_id} 浏览器关闭时忽略异常: {logger.summarize_exception(e)}")


async def main():
    modules = []
    if config.enableAutoCaptcha:
        print("===== Install Log =====")
        logger.info("正在检查依赖库...")
        modules = installer.start()
        logger.info("所有依赖库安装完成!")
    print("====== Login Log ======")
    async with async_playwright() as p:
        cookies = load_cookies(COOKIE_PATH)
        logger.info("正在扫描待学习资源...")
        jobs = await collect_jobs(p, cookies, modules)
        cookies = load_cookies(COOKIE_PATH) or cookies
        if not jobs:
            logger.info("没有扫描到待学习资源.")
            return

        job_queue = asyncio.Queue()
        for job in jobs:
            job_queue.put_nowait(job)
        worker_count = min(config.maxBrowserInstances, len(jobs))
        progress = OverallProgress(len(jobs))
        logger.info(f"扫描完成,待处理资源 {len(jobs)} 个,即将启动 {worker_count} 个浏览器实例.", shift=True)
        print("===== Runtime Log =====")
        progress.render()
        workers = [
            asyncio.create_task(worker_loop(worker_id, p, job_queue, cookies, progress, modules))
            for worker_id in range(1, worker_count + 1)
        ]
        await asyncio.gather(*workers)
        print()
    print("===== Task Finished =====")
    logger.info("所有课程已学习完毕!")


if __name__ == "__main__":
    print_banner()
    logger = Logger()
    try:
        print("====== Init Log ======")
        logger.info("程序启动中...")
        config = Config("configs.ini")
        if not config.course_urls:
            logger.error("未检测到有效网址或不支持此类网页,请检查配置文件!")
            time.sleep(2)
            sys.exit(-1)
        asyncio.run(main())
    except TargetClosedError as e:
        if "BrowserType.launch" in repr(e):
            logger.log_exception("浏览器相关流程异常结束.", e)
            logger.error("浏览器启动失败,请尝试重新启动!")
            logger.info("如果仍然无法启动,请修改配置文件并使用Chrome浏览器")
        else:
            logger.debug(f"浏览器关闭结束运行: {logger.summarize_exception(e)}")
    except Exception as e:
        logger.log_exception("程序运行时出现未处理异常.", e, shift=True)
        if isinstance(e, KeyError):
            logger.error(f"配置文件错误!")
        elif isinstance(e, FileNotFoundError):
            logger.error(f"依赖文件缺失: {e.filename},请重新安装程序!")
        elif isinstance(e, UnicodeDecodeError):
            logger.error("配置文件编码错误,保存时请选择UTF-8或GBK编码!")
        else:
            logger.error("系统出错,请检查后重新启动!")
    finally:
        logger.save()
        input("程序已结束,按Enter退出...")
