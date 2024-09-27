# Torrent Search Tool

`torrent_search.py` is a Python script that allows you to search for torrents on The Pirate Bay (using the `apibay.org` API) with keywords defined in a text file. The results are saved in an SQLite database and also exported to a CSV file for further analysis and reference.

## Features

- **Torrent search by keyword**: Input keywords in a text file, and the script will search for related torrents.
- **SQLite database**: Search results are stored in an SQLite database for later queries.
- **CSV export**: The search results are also exported to a CSV file with well-formatted column names.
- **Torrent details retrieval**: The script fetches additional details for each torrent, such as age, size, included files, and magnet links.
- **Support for multiple categories**: The tool classifies torrents into different categories, such as movies, applications, games, music, and more.

## Requirements

- Python 3.6 or higher
- Additional packages:
  - `requests`
  - `beautifulsoup4`
  - `pytz`
  - `sqlite3` (included in Python’s standard library)
  - `csv`
  - `argparse`
  - `logging`
  - `subprocess`

You can install the additional dependencies by running:

```bash
pip3 install requests beautifulsoup4 pytz
```

## Usage Instructions

1. **Prepare a keyword file**:
   Create a text file where each line contains a keyword to search for on The Pirate Bay. Example:

   ```txt
   movie
   software
   music
   ```

2. **Run the script**:
   To run the script, use the following command in the terminal:

   ```bash
   python3 torrent_search.py keywords.txt
   ```

   Where `keywords.txt` is the text file with the keywords. The script will search for the keywords, store the results in the `torrents.db` database, and export the results to a CSV file.

3. **Script options**:
   The script supports the following options:

   ```bash
   python3 torrent_search.py -h
   ```

   **Options**:
   - `-h, --help`: Displays the help message.
   - `file`: Text file with keywords for the search.

4. **File structure**:
   - **SQLite database (`torrents.db`)**: Stores the details of the torrents, including:
     - `id`: Unique torrent ID.
     - `name`: Torrent name.
     - `info_hash`: Torrent hash.
     - `leechers`: Number of leechers.
     - `seeders`: Number of seeders.
     - `num_files`: Number of files included in the torrent.
     - `size`: Torrent size.
     - `username`: Username of the uploader.
     - `added`: Addition date (Unix timestamp).
     - `added_utc`: Addition date in UTC format.
     - `status`: Torrent status (e.g., `vip`, `trusted`).
     - `category`: Category code.
     - `category_name`: Category name (e.g., Movies, Software).
     - `imdb`: IMDb ID, if available.
     - `torrent_age`: Torrent age.
     - `details`: List of files included in the torrent.
     - `magnet_link`: Magnet link.

   - **CSV**: Exports the search results to a CSV file in the format:

     ```csv
     id|name|info_hash|leechers|seeders|num_files|size|username|added|added_utc|status|category|category_name|imdb|torrent_age|details|magnet_link
     ```

## Usage Examples

1. **Search for torrents using keywords from a file**:

   ```bash
   python3 torrent_search.py keywords.txt
   ```

   Output:

   ```bash
   The folder Torrents already exists
   Processing keyword 'pistol'...
   [...]
   100 results saved in the torrents.db database
   Results have been exported to a CSV file called 2024-09-27_Results.csv
   ```

2. **Query the SQLite database**:

   ```bash
   sqlite3 torrents.db
   sqlite> SELECT * FROM torrents LIMIT 3;
   ```

   Example output:

   ```txt
   74591101|DungeonSex - Kink - Sophia Locke - Deep Connection, Sophia and Tommy Pistol 720p|8F600F390F079F16E0B4E49E133CA28854710CA3|3|12|1|2781502161|Yurievij|1707652679|2024-02-11 11:57:59|vip|505|HD - Movies||7 months||magnet:?xt=urn:btih:8f600f390f079f16e0b4e49e133ca28854710ca3&dn=DungeonSex+-+Deep+Connection+-+Sophia+Locke+and+Tommy+Pistol,+Feb+9,+2024_720p.mp4&tr=udp://tracker.openbittorrent.com:80&tr=udp://tracker.opentrackr.org:1337/announce
   ```

## Log

The script generates a detailed log of the operations performed, useful for debugging or reviewing program behavior. It uses `DEBUG` level by default, meaning it includes detailed debugging messages.

## Contributions

Contributions are welcome. If you have suggestions, bugs to report, or improvements to make, feel free to open an `issue` or submit a `pull request`.

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). See the [LICENSE](LICENSE) file for more details.
