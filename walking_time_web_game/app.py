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


# 把 OSRM 預估時間放大 1.6 倍，讓它比較接近現實開車時間
TIME_MULTIPLIER = 1.6


HEADERS = {
    "User-Agent": "driving-time-route-game/1.0"
}


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
    coord_text = ";".join(
        f"{lon},{lat}" for lat, lon in coords_in_order
    )

    url = f"https://router.project-osrm.org/route/v1/driving/{coord_text}"

    params = {
        "overview": "full",
        "geometries": "geojson"
    }

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()

    data = response.json()

    if data.get("code") != "Ok":
        raise ValueError("OSRM 無法計算其中一種開車路線")

    route = data["routes"][0]

    distance_m = route["distance"]
    duration_s = route["duration"]
    geometry = route["geometry"]

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

    best_result = None

    for order in itertools.permutations(range(n)):
        ordered_places = [places[i] for i in order]
        ordered_coords = [coords[i] for i in order]

        try:
            distance_m, duration_s, geometry = get_driving_route_for_order(
                ordered_coords
            )
        except Exception:
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

        # OSRM GeoJSON 是 [lon, lat]，Folium 要 [lat, lon]
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
                comment = "超準！"
            elif error_percent <= 25:
                comment = "不錯，很接近。"
            elif error_percent <= 50:
                comment = "還可以，但時間感有點偏。"
            else:
                comment = "差很多，再練習路感！"

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
