"""Treasury of Lives links for persons — a site-scoped web search of treasuryoflives.org
by name. The site is bot-walled (no server-side canonical-URL resolution) and its id-only
biography URLs 404 in the browser without a title slug, so a name search reliably lands on
the right biography and works for every person, authority-bound or not."""
from urllib.parse import quote_plus

from catalogue.db_store import connect
from catalogue.webui.web import create_app


def test_treasury_of_lives_redirect_and_links(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    name = "Losang Lungtog Tenzin Trinley"
    # the example from the bug report: even authority-bound, the old P4138 path 404'd.
    bound = db.execute("INSERT INTO person (primary_name, external_id) "
                       "VALUES (?, 'wikidata:Q2182680')", (name,)).lastrowid
    plain = db.execute("INSERT INTO person (primary_name) VALUES ('Modern Author')").lastrowid
    db.commit()
    with app.test_client() as c:
        # bound or not, we send the operator to a site-scoped search for the name
        r = c.get(f"/person/{bound}/treasuryoflives")
        assert r.status_code == 302
        loc = r.headers["Location"]
        assert "google.com/search" in loc
        assert quote_plus("site:treasuryoflives.org") in loc
        assert quote_plus(name) in loc

        r2 = c.get(f"/person/{plain}/treasuryoflives")
        assert r2.status_code == 302
        assert quote_plus("Modern Author") in r2.headers["Location"]

        # unknown person → 404
        assert c.get("/person/999999/treasuryoflives").status_code == 404
        # the person page still links to it
        assert b"/treasuryoflives" in c.get(f"/person/{bound}").data
