import os
import random
import time
import itertools
import requests
import folium

from flask import Flask, render_template, request, session
from regions import DEFAULT_REGION_ID, REGIONS

app = Flask(__name__)
app.secret_key = "change-this-secret-key"


DIFFICULTY_SETTINGS = {
    "easy": {
        "name": "簡單",
        "place_count": 2
    },
    "medium": {
        "name": "中等",
        "place_count": 3
    },
    "hard": {
        "name": "困難",
        "place_count": 4
    }
}


# Google Routes API 已經會回傳開車預估時間，所以不再額外放大。
TIME_MULTIPLIER = 1.0

GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_ROUTES_FIELD_MASK = (
    "routes.duration,"
    "routes.distanceMeters,"
    "routes.polyline.encodedPolyline"
)
DEMO_GOOGLE_MAPS_API_KEY = "AIzaSyCCgeL-moUPqlQJyNkbzZ9GmBguBcdVtHg"


HEADERS = {
    "User-Agent": "driving-time-route-game/1.0"
}


def load_env_file():
    """
    讀取專案根目錄的 .env，方便本機 demo 放 API key。
    已存在的環境變數不會被覆蓋。
    """
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

    if not os.path.exists(env_path):
        return

    with open(env_path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


def get_google_maps_api_key():
    """
    優先使用本機環境變數；沒有設定時，使用 demo 專用 API key。
    """
    return os.environ.get("GOOGLE_MAPS_API_KEY", DEMO_GOOGLE_MAPS_API_KEY)


def geocode(place_name):
    """
    用 Nominatim 把地點名稱轉成經緯度。
    回傳格式: [lat, lon]
    """
    url = "https://nominatim.openstreetmap.org/search"

    params = {
        "q": place_name,
        "format": "json",
        "limit": 1
    }

    response = requests.get(url, params=params, headers=HEADERS, timeout=10)
    response.raise_for_status()

    data = response.json()

    if not data:
        raise ValueError(f"找不到地點：{place_name}")

    lat = float(data[0]["lat"])
    lon = float(data[0]["lon"])

    return [lat, lon]


def decode_google_polyline(encoded_polyline):
    """
    把 Google encoded polyline 解碼成 GeoJSON LineString。
    Folium 畫線需要 [lat, lon]，但為了沿用原本 make_map 的轉換流程，
    這裡輸出 GeoJSON 格式的 [lon, lat]。
    """
    index = 0
    lat = 0
    lon = 0
    coordinates = []

    while index < len(encoded_polyline):
        result = 0
        shift = 0

        while True:
            value = ord(encoded_polyline[index]) - 63
            index += 1
            result |= (value & 0x1f) << shift
            shift += 5

            if value < 0x20:
                break

        lat += ~(result >> 1) if result & 1 else result >> 1

        result = 0
        shift = 0

        while True:
            value = ord(encoded_polyline[index]) - 63
            index += 1
            result |= (value & 0x1f) << shift
            shift += 5

            if value < 0x20:
                break

        lon += ~(result >> 1) if result & 1 else result >> 1
        coordinates.append([lon / 100000, lat / 100000])

    return {
        "type": "LineString",
        "coordinates": coordinates
    }


def coords_to_google_waypoint(coord):
    lat, lon = coord

    return {
        "location": {
            "latLng": {
                "latitude": lat,
                "longitude": lon
            }
        }
    }


def parse_google_duration(duration_text):
    if not duration_text.endswith("s"):
        raise ValueError(f"Google Routes API 回傳未知時間格式：{duration_text}")

    return float(duration_text[:-1])


def get_driving_route_for_order(coords_in_order):
    """
    給定一個拜訪順序，計算這個順序的開車總距離與總時間。

    coords_in_order 格式:
    [
        [lat, lon],
        [lat, lon],
        ...
    ]

    回傳:
    distance_m, duration_s, geometry
    """
    api_key = get_google_maps_api_key()

    if not api_key:
        raise ValueError("找不到 GOOGLE_MAPS_API_KEY，請先設定 Google Maps API key")

    body = {
        "origin": coords_to_google_waypoint(coords_in_order[0]),
        "destination": coords_to_google_waypoint(coords_in_order[-1]),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "polylineQuality": "OVERVIEW",
        "languageCode": "zh-TW",
        "units": "METRIC"
    }

    if len(coords_in_order) > 2:
        body["intermediates"] = [
            coords_to_google_waypoint(coord)
            for coord in coords_in_order[1:-1]
        ]

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": GOOGLE_ROUTES_FIELD_MASK
    }

    response = requests.post(
        GOOGLE_ROUTES_URL,
        json=body,
        headers=headers,
        timeout=15
    )
    response.raise_for_status()

    data = response.json()

    if not data.get("routes"):
        raise ValueError("Google Routes API 無法計算其中一種開車路線")

    route = data["routes"][0]

    distance_m = route["distanceMeters"]
    duration_s = parse_google_duration(route["duration"])
    geometry = decode_google_polyline(route["polyline"]["encodedPolyline"])

    return distance_m, duration_s, geometry


def find_shortest_route(places, coords):
    """
    嘗試所有拜訪順序，找出總開車時間最短的路線。

    places: 地點名稱 list
    coords: 座標 list

    回傳:
    {
        "best_order": [...],
        "best_places": [...],
        "best_coords": [...],
        "distance_m": ...,
        "duration_s": ...,
        "adjusted_duration_s": ...,
        "geometry": ...
    }
    """
    n = len(places)

    if not get_google_maps_api_key():
        raise ValueError("找不到 GOOGLE_MAPS_API_KEY，請先設定 Google Maps API key")

    best_result = None
    last_error = None

    for order in itertools.permutations(range(n)):
        ordered_places = [places[i] for i in order]
        ordered_coords = [coords[i] for i in order]

        try:
            distance_m, duration_s, geometry = get_driving_route_for_order(
                ordered_coords
            )
        except Exception as e:
            last_error = e
            continue

        if best_result is None or duration_s < best_result["duration_s"]:
            best_result = {
                "best_order": list(order),
                "best_places": ordered_places,
                "best_coords": ordered_coords,
                "distance_m": distance_m,
                "duration_s": duration_s,
                "adjusted_duration_s": duration_s * TIME_MULTIPLIER,
                "geometry": geometry
            }

    if best_result is None:
        if last_error:
            raise ValueError(f"所有可能路線都無法計算：{last_error}")

        raise ValueError("所有可能路線都無法計算")

    return best_result


def make_map(places, coords, geometry=None, best_places=None):
    """
    產生地圖。
    如果還沒揭曉答案，可以只顯示景點。
    如果已經揭曉答案，顯示最佳開車路線。
    """
    if not places or not coords:
        return None

    center_lat = sum(coord[0] for coord in coords) / len(coords)
    center_lon = sum(coord[1] for coord in coords) / len(coords)

    m = folium.Map(location=[center_lat, center_lon], zoom_start=13)

    labels = ["A", "B", "C", "D"]

    for i, (place, coord) in enumerate(zip(places, coords)):
        lat, lon = coord

        folium.Marker(
            [lat, lon],
            popup=f"{labels[i]}: {place}",
            tooltip=f"{labels[i]} 點"
        ).add_to(m)

    if geometry:
        coordinates = geometry["coordinates"]

        # GeoJSON 是 [lon, lat]，Folium 要 [lat, lon]
        route_points = [[lat, lon] for lon, lat in coordinates]

        folium.PolyLine(
            route_points,
            tooltip="最短開車路線",
            weight=5
        ).add_to(m)

    if best_places:
        order_text = " → ".join(best_places)

        title_html = f"""
        <div style="
            position: fixed;
            top: 10px;
            left: 50px;
            z-index: 9999;
            background-color: white;
            padding: 12px;
            border: 2px solid black;
            font-size: 14px;
            max-width: 420px;
        ">
            <b>最佳拜訪順序</b><br>
            {order_text}
        </div>
        """

        m.get_root().html.add_child(folium.Element(title_html))

    return m._repr_html_()


def should_show_map_before_guess(difficulty):
    """
    簡單模式：猜之前就顯示地圖。
    中等、困難模式：猜完之後才顯示地圖。
    """
    return difficulty == "easy"


def get_region(region_id):
    """
    依照地區代號取得地區資料。
    """
    if region_id not in REGIONS:
        region_id = DEFAULT_REGION_ID

    return region_id, REGIONS[region_id]


def get_template_data(map_html=None, result=None, error=None):
    """
    整理模板需要的共用資料，避免每個回傳頁面都重複寫一大段。
    """
    difficulty = session.get("difficulty", "easy")
    region_id = session.get("region", DEFAULT_REGION_ID)
    region_id, region = get_region(region_id)

    return {
        "places": session.get("places"),
        "difficulty": difficulty,
        "difficulty_name": DIFFICULTY_SETTINGS[difficulty]["name"],
        "region": region_id,
        "region_name": region["name"],
        "regions": REGIONS,
        "map_html": map_html,
        "result": result,
        "error": error,
        "show_hidden_map_note": False if result else not should_show_map_before_guess(difficulty),
        "time_multiplier": TIME_MULTIPLIER
    }


def create_new_question(difficulty, region_id):
    """
    建立新題目。
    """
    if difficulty not in DIFFICULTY_SETTINGS:
        difficulty = "easy"

    region_id, region = get_region(region_id)
    place_count = DIFFICULTY_SETTINGS[difficulty]["place_count"]
    places = region["places"]

    if len(places) < place_count:
        raise ValueError(f"{region['name']} 的地點數量不足，無法產生這個難度的題目")

    selected_places = random.sample(places, place_count)

    selected_coords = []

    for place in selected_places:
        coord = geocode(place)
        selected_coords.append(coord)

        # 避免太快連續打 Nominatim
        time.sleep(1)

    session["difficulty"] = difficulty
    session["region"] = region_id
    session["places"] = selected_places
    session["coords"] = selected_coords


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error = None

    if request.method == "GET":
        difficulty = "easy"
        region = DEFAULT_REGION_ID

        try:
            create_new_question(difficulty, region)

            if should_show_map_before_guess(difficulty):
                map_html = make_map(
                    session["places"],
                    session["coords"]
                )
            else:
                map_html = None

        except Exception as e:
            error = str(e)
            map_html = None

        return render_template("index.html", **get_template_data(map_html, result, error))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "new":
            difficulty = request.form.get("difficulty", "easy")
            region = request.form.get("region", DEFAULT_REGION_ID)

            try:
                create_new_question(difficulty, region)

                if should_show_map_before_guess(difficulty):
                    map_html = make_map(
                        session["places"],
                        session["coords"]
                    )
                else:
                    map_html = None

            except Exception as e:
                error = str(e)
                map_html = None

            return render_template("index.html", **get_template_data(map_html, result, error))

        try:
            user_guess_min = float(request.form["guess"])

            places = session["places"]
            coords = session["coords"]

            shortest = find_shortest_route(places, coords)

            original_time_min = shortest["duration_s"] / 60
            adjusted_time_min = shortest["adjusted_duration_s"] / 60
            real_distance_km = shortest["distance_m"] / 1000

            error_min = abs(user_guess_min - adjusted_time_min)
            error_percent = error_min / adjusted_time_min * 100

            score = max(0, round(100 - error_percent))

            if error_percent <= 10:
                comment = "接近100分！"
            elif error_percent <= 25:
                comment = "再加油"
            elif error_percent <= 50:
                comment = "你看得懂地圖嗎？"
            else:
                comment = "別玩了吧，好好念書吧。"

            result = {
                "user_guess_min": user_guess_min,
                "original_time_min": original_time_min,
                "adjusted_time_min": adjusted_time_min,
                "real_distance_km": real_distance_km,
                "error_min": error_min,
                "error_percent": error_percent,
                "score": score,
                "comment": comment,
                "best_places": shortest["best_places"]
            }

            # 猜完之後一定顯示地圖，包含中等和困難模式
            map_html = make_map(
                places,
                coords,
                geometry=shortest["geometry"],
                best_places=shortest["best_places"]
            )

        except Exception as e:
            error = str(e)

            difficulty = session.get("difficulty", "easy")

            if should_show_map_before_guess(difficulty):
                map_html = make_map(
                    session.get("places", []),
                    session.get("coords", [])
                )
            else:
                map_html = None

        return render_template("index.html", **get_template_data(map_html, result, error))


if __name__ == "__main__":
    app.run(debug=True)
