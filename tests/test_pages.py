def test_home_renders_design_shell(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'data-text="DOGGFATHER"' in html
    assert "/static/css/app.css" in html
    assert "Build the platform" in html


def test_static_assets_are_served(client):
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert "--pink: #FF3D6E" in css.text
    assert client.get("/static/js/app.js").status_code == 200


def test_browser_404_is_a_styled_page(client):
    response = client.get("/nowhere", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "Signal lost" in response.text


def test_api_404_stays_json(client):
    response = client.get("/api/nowhere")
    assert response.headers["content-type"].startswith("application/json")


def test_user_text_is_escaped_by_paragraph_filter():
    from doggfather.web.templating import paragraphs

    html = str(paragraphs("<script>alert(1)</script>\n\nsecond"))
    assert "<script>" not in html
    assert html.count("<p>") == 2
