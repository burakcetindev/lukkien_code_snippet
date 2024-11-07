from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from bs4 import BeautifulSoup
import time
import logging
from tqdm import tqdm
import pandas as pd
import argparse

# Configure logging
logger = logging.getLogger('GhostFollow')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

class GhostFollow:
    def __init__(self, chromedriver_path, username, password):
        self.driver = webdriver.Chrome(chromedriver_path)
        self.username = username
        self.password = password
        self.people_followed = 0
        self.driver.get("https://www.instagram.com/")
        self._login()

    def _wait_for_element(self, by, value, timeout=10):
        return WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable((by, value)))

    def _login(self):
        logger.info("Logging in to Instagram")
        username_input = self._wait_for_element(By.NAME, 'username')
        password_input = self._wait_for_element(By.NAME, 'password')
        username_input.send_keys(self.username)
        password_input.send_keys(self.password)
        login_button = self._wait_for_element(By.CSS_SELECTOR, "button[type='submit']")
        login_button.click()
        
        # Bypass "Save Login Info" and "Turn On Notifications" popups
        self._handle_post_login_popups()

    def _handle_post_login_popups(self):
        try:
            not_now_button = self._wait_for_element(By.XPATH, '//button[contains(text(), "Not Now")]')
            not_now_button.click()
            time.sleep(1)
            not_now_button = self._wait_for_element(By.XPATH, '//button[contains(text(), "Not Now")]')
            not_now_button.click()
        except Exception as e:
            logger.warning(f"Post-login popups not found: {e}")

    def search_hashtag(self, hashtag):
        searchbox = self._wait_for_element(By.XPATH, "//input[@placeholder='Search']")
        searchbox.clear()
        searchbox.send_keys(f'#{hashtag}')
        logger.info(f'Searching by #{hashtag}')
        time.sleep(3)
        searchbox.send_keys(Keys.ENTER)
        time.sleep(2)
        searchbox.send_keys(Keys.ENTER)
        time.sleep(3)

    def collect_post_links(self, scroll_limit, scroll_pause=5):
        post_links = set()
        last_height = self.driver.execute_script("return document.body.scrollHeight")

        for _ in tqdm(range(scroll_limit), desc="Scrolling"):
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(scroll_pause)
            
            links = self.driver.find_elements_by_tag_name('a')
            for link in links:
                href = link.get_attribute('href')
                if href and '.com/p/' in href:
                    post_links.add(href)

            new_height = self.driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        logger.info(f"Collected {len(post_links)} unique post links")
        return list(post_links)

    def scrape_post_data(self, links):
        data = []
        for rank, link in enumerate(tqdm(links, desc="Scraping Posts")):
            self.driver.get(link)
            time.sleep(2)

            is_video = self._check_if_video()
            if is_video:
                data.append(self._scrape_video_data(link, rank))
            else:
                data.append(self._scrape_image_data(link, rank))

        logger.info(f"Scraped data for {len(data)} posts")
        return data

    def _check_if_video(self):
        page_source = self.driver.page_source
        return '"is_video":true' in page_source

    def _scrape_image_data(self, link, rank):
        return {
            "date": self._get_post_date(),
            "type": "image",
            "user": self._get_user(),
            "subtitles": self._get_subtitles(),
            "image_description": self._get_image_description(),
            "likes": self._get_likes(),
            "views": None,
            "rank": rank,
            "link": link
        }

    def _scrape_video_data(self, link, rank):
        return {
            "date": self._get_post_date(),
            "type": "video",
            "user": self._get_user(),
            "subtitles": self._get_subtitles(),
            "views": self._get_views(),
            "likes": None,
            "rank": rank,
            "link": link
        }

    def _get_user(self):
        user_link = self.driver.find_element_by_xpath('//header//span/a')
        return user_link.get_attribute('href').split('/')[-2]

    def _get_subtitles(self):
        subtitle_element = self.driver.find_element_by_xpath('//article//span')
        return BeautifulSoup(subtitle_element.get_attribute('innerHTML')).get_text()

    def _get_image_description(self):
        images = self.driver.find_elements_by_tag_name('img')
        if images:
            return images[1].get_attribute('alt').split("Image may contain: ")[-1]
        return ''

    def _get_likes(self):
        likes_element = self.driver.find_element_by_xpath('//section//button/span')
        return likes_element.get_attribute('innerHTML')

    def _get_views(self):
        views_element = self.driver.find_element_by_xpath('//section/span/span')
        return views_element.get_attribute('innerHTML')

    def _get_post_date(self):
        date_element = self.driver.find_element_by_xpath('//time')
        return date_element.get_attribute('datetime')

    def search_accounts(self, account_username):
        searchbox = self._wait_for_element(By.CSS_SELECTOR, 'input')
        searchbox.clear()
        searchbox.send_keys(account_username)
        time.sleep(2)
        searchbox.send_keys(Keys.ENTER)
        time.sleep(1)
        searchbox.send_keys(Keys.ENTER)

    def scroll_and_follow(self, scroll_limit=100):
        self._open_followers_list()
        for _ in range(scroll_limit):
            time.sleep(2)
            followers_list = self.driver.find_element_by_xpath('//div[@role="dialog"]//ul')
            self.driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", followers_list)

        self._follow_users()

    def _open_followers_list(self):
        followers_button = self.driver.find_element_by_xpath('//header/section/ul/li[2]/a')
        followers_button.click()
        time.sleep(2)

    def _follow_users(self):
        buttons = self.driver.find_elements_by_xpath("//button[text()='Follow']")
        for button in buttons:
            button.click()
            self.people_followed += 1
            time.sleep(1)

    def unfollow_all(self):
        profile_button = self.driver.find_element_by_class_name("_6q-tv")
        profile_button.click()
        time.sleep(1)
        following_button = self.driver.find_elements_by_class_name("-nal3")[2]
        following_button.click()
        
        self._unfollow_users()

    def _unfollow_users(self):
        buttons = self.driver.find_elements_by_xpath("//button[text()='Following']")
        for button in buttons:
            button.click()
            time.sleep(1)
            confirm_unfollow = self._wait_for_element(By.XPATH, "//button[text()='Unfollow']")
            confirm_unfollow.click()
            time.sleep(1)

    def save_data(self, data, output_file):
        df = pd.DataFrame(data)
        df.to_csv(output_file, index=False)
        logger.info(f"Data saved to {output_file}")

    def close(self):
        self.driver.quit()

def main():
    parser = argparse.ArgumentParser(description="Instagram scraper and automation tool.")
    parser.add_argument('-u', '--username', required=True, help="Instagram username")
    parser.add_argument('-p', '--password', required=True, help="Instagram password")
    parser.add_argument('-c', '--chromedriver', required=True, help="Path to Chromedriver")
    parser.add_argument('-t', '--tag', help="Hashtag to search")
    parser.add_argument('-a', '--account', help="Account username to search")
    parser.add_argument('-n', '--nscrolls', type=int, default=100, help="Number of scrolls")
    parser.add_argument('-o', '--output', default='output.csv', help="Output file name")
    args = parser.parse_args()

    bot = GhostFollow(args.chromedriver, args.username, args.password)

    if args.tag:
        bot.search_hashtag(args.tag)
        post_links = bot.collect_post_links(args.nscrolls)
        data = bot.scrape_post_data(post_links)
        bot.save_data(data, args.output)
    
    if args.account:
        bot.search_accounts(args.account)
        bot.scroll_and_follow()

    bot.close()

if __name__ == "__main__":
    main()