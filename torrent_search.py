import requests
import json
import sqlite3
import datetime
import pytz
import argparse
import csv
from urllib.parse import quote
from bs4 import BeautifulSoup
import logging
import subprocess
import re
import os

logging.basicConfig(level=logging.DEBUG)

CATEGORIES = {
    100: "Audio", 101: "Music", 102: "Audio books", 103: "Sound clips", 104: "FLAC", 199: "Other",
    200: "Video", 201: "Movies", 202: "Movies DVDR", 203: "Music videos", 204: "Movie clips",
    205: "TV shows", 206: "Handheld", 207: "HD - Movies", 208: "HD - TV shows", 209: "3D", 210: "CAM/TS", 211: "UHD/4k - Movies", 212: "UHD/4k - TV shows", 299: "Other",
    300: "Applications", 301: "Windows", 302: "Mac", 303: "UNIX", 304: "Handheld", 305: "IOS (iPad/iPhone)",
    306: "Android", 399: "Other OS",
    400: "Games", 401: "PC", 402: "Mac", 403: "PSx", 404: "XBOX360", 405: "Wii", 406: "Handheld",
    407: "IOS (iPad/iPhone)", 408: "Android", 499: "Other",
    500: "Porn", 501: "Movies", 502: "Movies DVDR", 503: "Pictures", 504: "Games",
    505: "HD - Movies", 506: "Movie clips", 507: "UHD/4k - Movies", 599: "Other",
    600: "Other", 601: "E-books", 602: "Comics", 603: "Pictures", 604: "Covers", 605: "Physibles", 699: "Other"
}

folder = "Torrents"

path = os.path.join(os.getcwd(), folder)

if os.path.exists(path):
    print(f"The folder {folder} already exists")
else:
    os.mkdir(path)
    print(f"The folder {folder} has been created")

def search_torrents(term):
    term = quote(f'"{term}"')
    url = f"https://apibay.org/q.php?q={term}"
    response = requests.get(url)
    if response.status_code == 200:
        results = json.loads(response.content)
        return results
    else:
        return []

def save_torrents(results):
    connection = sqlite3.connect("torrents.db")
    cursor = connection.cursor()
    # Adding "details" column to torrents table
    cursor.execute("CREATE TABLE IF NOT EXISTS torrents (id TEXT, name TEXT, info_hash TEXT UNIQUE, leechers TEXT, seeders TEXT, num_files TEXT, size TEXT, username TEXT, added TEXT, added_utc TEXT, status TEXT, category TEXT, category_name TEXT, imdb TEXT, torrent_age TEXT, details TEXT, magnet_link TEXT)")
    utc = pytz.utc
    data = []
    for result in results:
        if result["id"] == '0' and result["name"] == 'No results returned':
            continue
        added_unix = int(result["added"])
        added_utc = datetime.datetime.fromtimestamp(added_unix, tz=utc).strftime('%Y-%m-%d %H:%M:%S')
        category_code = int(result["category"])
        category_name = CATEGORIES.get(category_code, "")
        source_code = get_source_code(result["info_hash"])
        try:
            torrent_data = format_response(source_code)
            torrent_age = torrent_data["age"]
            torrent_details = torrent_data["details"]  # Get torrent details
            magnet_link = torrent_data["magnet"]
        except Exception as e:
            logging.error(f"Error formatting response for infohash {result['info_hash']}: {e}")
            torrent_age = 'N/A'
            torrent_details = 'N/A'  # Default value for torrent details
            magnet_link = 'N/A'
        # Add the torrent details to the data tuple
        data.append((result["id"], result["name"], result["info_hash"], result["leechers"], result["seeders"], result["num_files"], result["size"], result["username"], result["added"], added_utc, result["status"], result["category"], category_name, result["imdb"],
torrent_age,
torrent_details,
magnet_link))
    cursor.executemany("INSERT OR IGNORE INTO torrents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
    connection.commit()
    connection.close()

def read_keywords(file):
    keywords = []
    with open(file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                keywords.append(line)
    return keywords

def export_csv():
    connection = sqlite3.connect("torrents.db")
    cursor = connection.cursor()
    cursor.execute("SELECT * FROM torrents")
    data = cursor.fetchall()
    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    csv_name = f"{today_str}_Results.csv"
    with open(csv_name, "w", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        # Adding "details" column to csv file
        writer.writerow(["id", "name", "info_hash", "leechers", "seeders", 
"num_files","size","username","added","added_utc","status","category","category_name","imdb","torrent_age","details","magnet_link"])
        writer.writerows(data)
    connection.close()

def get_source_code(infohash):
    command = ["curl", f"https://btdig.com/{infohash}"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    source_code = result.stdout
    return source_code

def format_response(source_code):
    soup = BeautifulSoup(source_code, "html.parser")
    element = soup.find("h1")
    if element is not None:
        name = element.text
    else:
        name = "Unknown"
    element = soup.find("span", text="Size:")
    if element is not None:
        size = element.next_sibling.string.strip()
    else:
        size = "Unknown"
    element = soup.find("meta", attrs={"name": "description"})
    if element is not None:
        age = element["content"]
        match = re.search(r"Age: (\d+ \w+)", age)
        if match:
            age = match.group(1) 
        else:
            age = "Unknown"
    else:
        age = "Unknown"
    details = []
    elements = soup.find_all("div", class_="fa fa-folder-open") + soup.find_all("div", class_="fa fa-file-o") + soup.find_all("div", class_="fa fa-file-image-o") + soup.find_all("div", class_="fa fa-file-word-o") + soup.find_all("div", class_="fa fa-file-pdf-o")
    for element in elements:
        file_name = element.text.strip()
        file_size = element.find_next_sibling("span").text.strip()
        detail = f"{file_name} ({file_size})"
        details.append(detail)
    details = ", ".join(details) 
    element = soup.find("a", href=lambda x: x and x.startswith("magnet:"))
    if element is not None:
        magnet = element["href"]
    else:
        magnet = "Unknown"
    data = {"name": name, "size": size, "age": age, "details": details, "magnet": magnet} 
    return data

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search and save torrents from a text file with keywords")
    parser.add_argument("file", help="The name of the text file with keywords (one per line)")
    args = parser.parse_args()
    file = args.file
    keywords = read_keywords(file)
    for keyword in keywords:
        print(f"Processing keyword {keyword}...")
        results = search_torrents(keyword)
        save_torrents(results)
        print(f"{len(results)} results saved in the torrents.db database")
    export_csv()
    print("Results have been exported to a CSV file called results.csv")
