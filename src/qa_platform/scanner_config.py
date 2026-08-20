from __future__ import annotations

SLOW_PAGE_THRESHOLD_SECONDS = 3.0
MAX_ACTIONS_PER_PAGE = 6

ACTION_TIMEOUT_MS = 1500

HIGH_VALUE_PATH_PARTS = {
    "about",
    "pricing",
    "product",
    "products",
    "service",
    "services",
    "docs",
    "documentation",
    "contact",
    "signup",
    "register",
    "cart",
    "basket",
    "bag",
    "checkout",
    "support",
    "help",
    "blog",
    "careers",
    "features",
    "solutions",
    "search",
    "deals",
    "offers",
    "sale",
    "bestseller",
    "bestsellers",
    "category",
    "categories",
    "shop",
    "store",
    "collections",
    "account",
    "orders",
}
HIGH_VALUE_ACTION_TERMS = {
    "search",
    "shop",
    "deals",
    "offers",
    "sale",
    "bestseller",
    "bestsellers",
    "product",
    "products",
    "category",
    "categories",
    "cart",
    "basket",
    "bag",
    "checkout",
    "buy",
    "order",
    "orders",
    "sign in",
    "signin",
    "login",
    "account",
    "wishlist",
    "location",
    "delivery",
    "update location",
    "see more",
    "explore all",
    "see all",
    "view all",
    "customer service",
    "support",
    "help",
}
PAGE_AREA_PRIORITY = {
    "header": 0,
    "nav": 1,
    "hero": 2,
    "main": 3,
    "card": 4,
    "carousel": 5,
    "footer": 8,
    "other": 6,
}
SECTION_TEST_ORDER = ("header", "nav", "hero", "card", "carousel", "main", "other", "footer")
SECTION_ACTION_LIMITS = {
    "header": 3,
    "nav": 3,
    "hero": 2,
    "card": 3,
    "carousel": 2,
    "main": 2,
    "other": 0,
    "footer": 0,
}
PRIMARY_ACTION_TERMS = {
    "search",
    "cart",
    "basket",
    "bag",
    "sign in",
    "signin",
    "login",
    "account",
    "location",
    "delivery",
    "update location",
    "all",
    "menu",
    "category",
    "categories",
}
SECONDARY_ACTION_TERMS = {
    "deals",
    "offers",
    "sale",
    "bestseller",
    "bestsellers",
    "product",
    "products",
    "see more",
    "explore all",
    "see all",
    "view all",
}
LOW_VALUE_ACTION_TERMS = {
    "privacy notice",
    "conditions of use",
    "interest-based ads",
    "your product safety alerts",
    "100% purchase protection",
    "main content",
    "help",
    "gift cards",
    "wishlist",
    "find a wishlist",
    "create your wishlist",
    "subscribe & save",
    "recalls and product safety alerts",
    "brand-logo",
    "explore more",
    "sort by",
}
LOW_VALUE_LINK_PATH_PARTS = {
    "wishlist",
    "gp",
    "hz",
    "footer",
    "privacy",
    "conditions",
    "legal",
    "help",
    "customer",
    "safety",
    "alerts",
    "gift-cards",
    "giftcards",
    "gift",
    "gift-card-store",
    "business",
    "create-invitation",
    "cv",
    "get",
    "showroom",
    "auto-deliveries",
    "autodeliveries",
    "r",
    "flights",
    "outlet",
    "discover",
}
WEAK_ACTION_LABELS = {
    "red", "blue", "green", "black", "white", "gold", "pink", "yellow", "silver", "grey", "gray",
    "brown", "beige", "purple", "coralred", "navy", "orange", "maroon", "cream", "multicolor",
    "off white", "off-white", "turquoise", "lime", "olive", "teal", "peach", "tan", "khaki",
    "english", "hindi", "language", "languages", "no hero v en in", "link", "close menu",
    "start here", "privacy notice", "conditions of use", "terms of use", "shipping", "sitemap",
    "recalls and product safety alerts", "brand logo", "brand-logo", "sort by", "explore more",
    "next slide", "previous slide", "submit",
}
HIGH_PRIORITY_LINK_TERMS = {
    "search", "item", "items", "dp", "cart", "basket", "bag",
    "checkout", "deal", "deals", "offer", "offers", "sale", "bestseller", "bestsellers",
    "category", "categories", "collection", "collections", "grocery", "supermart",
}
UTILITY_LINK_TERMS = {
    "corporate", "company", "information", "investor", "press", "careers", "jobs",
    "privacy", "terms", "conditions", "legal", "policy", "policies", "security", "compliance",
    "sitemap", "help", "helpcentre", "helpcenter", "faq", "payments",
    "payment", "shipping", "cancellation", "returns", "refund", "mobile", "apps", "app",
    "gift", "giftcard", "giftcards", "travel", "flight", "flights", "plus", "subscription",
    "suggestion", "searchsuggestion", "download",
}
SUPPORT_LINK_TERMS = {"support", "help", "helpcentre", "helpcenter", "faq", "customer", "customer-service"}
CONTENT_LINK_TERMS = {"blog", "blogs", "news", "article", "articles", "story", "stories", "press"}
SPECIAL_SERVICE_LINK_TERMS = {
    "gift", "giftcard", "giftcards", "flight", "flights", "travel", "mobile", "apps", "app",
    "minutes", "subscription", "plus", "download", "suggestion", "searchsuggestion",
}
LEGAL_FOOTER_LINK_TERMS = {
    "privacy", "terms", "conditions", "legal", "policy", "policies", "security", "compliance",
    "sitemap", "payments", "payment", "shipping", "cancellation", "returns", "refund",
    "corporate", "company", "information", "investor", "press", "careers", "jobs",
}
VARIANT_ACTION_TERMS = {
    "kg", "g", "gram", "grams", "size", "pouch", "pack", "pcs", "piece", "pieces", "cm", "mm",
    "inch", "inches", "litre", "liter", "ml", "colour", "color", "pattern",
}
NON_NAV_UI_TERMS = {"next slide", "previous slide", "carousel", "dropdown", "sort by", "filter", "submit"}
REVIEW_LABEL_TERMS = {
    "review", "reviews", "rating", "ratings", "rated", "good", "bad", "excellent",
    "awesome", "fabulous", "amazing", "great", "quality", "love", "useful", "nice",
    "fragrance", "stars", "granules", "must buy",
}
PRODUCT_METADATA_LABELS = {
    "brand store", "visit brand store", "product reviews", "customer support",
    "no cash on delivery", "cash on delivery", "retry", "try again",
    "retry in", "see all reviews", "read reviews",
}
RETRY_ACTION_TERMS = {"retry", "try again", "reload", "refresh", "again in"}
