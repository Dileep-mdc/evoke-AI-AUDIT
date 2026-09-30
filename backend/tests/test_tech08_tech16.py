from app.parameters.technical import _homepage_organization, has_fixed_width_layout


def test_a_responsive_theme_is_not_a_fixed_width_layout():
    """The old test fired on "min-width:" and "1200px" anywhere in the HTML, which every
    responsive theme satisfies -- Evoke's homepage lost 20 points to exactly this CSS."""
    css = ".site-content>.ast-container{max-width:1200px;}}@media (min-width:922px){.single-post{}}"
    assert not has_fixed_width_layout(css)
    assert not has_fixed_width_layout("@media (min-width:1200px){.a{}}")
    assert not has_fixed_width_layout("@media screen and ( min-width: 1440px ){.a{}}")
    assert not has_fixed_width_layout("td{min-width:120px}")
    assert not has_fixed_width_layout("--min-width:1200px")


def test_a_declared_fixed_page_width_is_flagged():
    assert has_fixed_width_layout("body{min-width:1200px}")
    assert has_fixed_width_layout(".wrapper { min-width : 1024px; }")


def _block(data):
    return {"ok": True, "data": data}


def test_the_organization_is_merged_across_nodes_that_describe_it():
    """Evoke's homepage publishes name/logo/sameAs in its @graph node and url/contactPoint
    on the Service provider. Reading only the first node scored 60 for a site that has 100."""
    blocks = [
        _block({"@graph": [
            {"@type": "Organization", "@id": "https://x.com/#organization", "name": "Evoke Technologies",
             "logo": {"@type": "ImageObject"}, "sameAs": ["https://linkedin.com/x"]},
            {"@type": "WebSite", "url": "https://x.com/"},
        ]}),
        _block({"@type": "Service", "name": "AI", "provider": {
            "@type": "Organization", "name": "Evoke Technologies", "url": "https://x.com",
            "contactPoint": {"@type": "ContactPoint", "url": "https://x.com/contact"}}}),
    ]
    org = _homepage_organization(blocks)
    assert all(org.get(f) for f in ("name", "url", "logo", "sameAs", "contactPoint"))


def test_a_different_organization_is_not_merged_in():
    """A client or partner named in the markup must not lend the site its fields."""
    blocks = [
        _block({"@type": "Organization", "name": "Evoke Technologies", "logo": "l.png"}),
        _block({"@type": "Article", "publisher": {"@type": "Organization", "name": "Some Publisher",
                                                  "url": "https://pub.example", "telephone": "123"}}),
    ]
    org = _homepage_organization(blocks)
    assert org.get("name") == "Evoke Technologies"
    assert not org.get("url") and not org.get("telephone")


def test_no_organization_node_means_none():
    assert _homepage_organization([_block({"@type": "WebSite", "url": "https://x.com"})]) is None
