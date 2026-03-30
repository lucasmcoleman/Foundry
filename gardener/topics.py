"""
Comprehensive topic taxonomy for NC gardening synthetic data generation.

Each topic has:
- category: broad domain
- subcategory: specific area
- prompts: diverse user question templates (with {placeholders} for variation)
- context_vars: dicts of possible values to fill placeholders
- difficulty: beginner / intermediate / advanced
- multi_turn: whether to generate follow-up conversation turns

The generator picks from these to create diverse, realistic training conversations.
"""

# ---------------------------------------------------------------------------
# Placeholder value pools (reused across topics)
# ---------------------------------------------------------------------------

NC_ZONES = ["6b", "7a", "7b", "8a"]
NC_REGIONS = [
    "western mountains near Asheville",
    "foothills around Morganton",
    "Charlotte area Piedmont",
    "Raleigh-Durham Triangle area",
    "Greensboro/Triad Piedmont",
    "Sandhills around Southern Pines",
    "Fayetteville area",
    "Wilmington coastal plain",
    "Outer Banks",
    "eastern NC near Greenville",
]
SEASONS = ["early spring", "late spring", "summer", "late summer", "early fall", "fall", "winter"]
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
EXPERIENCE_LEVELS = ["complete beginner", "first-year gardener", "intermediate gardener", "experienced gardener"]
SOIL_TYPES = ["heavy red clay", "sandy loam", "rocky mountain soil", "compacted clay", "sandy coastal soil", "silty river bottom soil"]

COMMON_VEGETABLES = [
    "tomatoes", "peppers", "squash", "zucchini", "cucumbers", "beans",
    "peas", "lettuce", "kale", "spinach", "collards", "Swiss chard",
    "carrots", "beets", "radishes", "turnips", "sweet potatoes",
    "Irish potatoes", "onions", "garlic", "corn", "okra", "eggplant",
    "watermelon", "cantaloupe", "pumpkins", "winter squash",
    "broccoli", "cauliflower", "cabbage", "Brussels sprouts",
    "snap peas", "lima beans", "southern peas (cowpeas)",
    "herbs (basil, cilantro, dill, parsley)",
]

COMMON_FRUITS = [
    "blueberries", "strawberries", "blackberries", "raspberries",
    "muscadine grapes", "figs", "peaches", "apples", "pears",
    "persimmons", "pawpaws", "elderberries", "mulberries",
]

COMMON_PESTS = [
    "Japanese beetles", "squash vine borers", "tomato hornworms",
    "aphids", "cabbage worms", "cucumber beetles", "flea beetles",
    "squash bugs", "stink bugs", "whiteflies", "spider mites",
    "fire ants", "slugs", "deer", "rabbits", "voles",
    "groundhogs", "armadillos",
]

COMMON_DISEASES = [
    "early blight", "late blight", "Southern blight",
    "powdery mildew", "downy mildew", "bacterial wilt",
    "Septoria leaf spot", "anthracnose", "fusarium wilt",
    "verticillium wilt", "root-knot nematodes", "damping off",
    "blossom end rot", "black rot on grapes/brassicas",
    "brown rot on stone fruit", "cedar-apple rust",
]

COVER_CROPS = [
    "crimson clover", "winter rye", "Austrian winter peas",
    "hairy vetch", "buckwheat", "cowpeas", "sorghum-sudan grass",
    "daikon radish (tillage radish)", "white clover", "red clover",
    "oats", "winter wheat", "triticale",
]

AMENDMENTS = [
    "compost", "worm castings", "aged manure", "leaf mold",
    "wood ash", "lime (calcitic)", "dolomitic lime", "sulfur",
    "greensand", "rock phosphate", "bone meal", "blood meal",
    "feather meal", "alfalfa meal", "kelp meal", "fish emulsion",
    "azomite", "gypsum", "biochar", "humic acid",
    "mycorrhizal inoculant",
]

BED_SIZES = [
    "4x4 raised bed", "4x8 raised bed", "4x12 raised bed",
    "3x6 raised bed", "10x10 in-ground plot", "20x20 in-ground plot",
    "half-acre plot", "quarter-acre yard", "small apartment balcony",
    "200 square feet total", "500 square feet total",
]

# ---------------------------------------------------------------------------
# Topic definitions
# ---------------------------------------------------------------------------

TOPICS = [
    # =========================================================================
    # SOIL SCIENCE & BUILDING
    # =========================================================================
    {
        "category": "Soil Science",
        "subcategory": "Soil Testing",
        "difficulty": "beginner",
        "prompts": [
            "I just moved to {region} and want to start a garden. How do I get my soil tested?",
            "I got my soil test results back from NC State. Can you help me understand what they mean? My pH is {ph}, P-I is {p_index}, K-I is {k_index}, and organic matter is {om}%.",
            "My soil test says my CEC is {cec} and base saturation is {base_sat}%. What does that tell me about my soil?",
            "How often should I test my soil, and should I test differently for different beds?",
            "What's the difference between the free NC State soil test and the ones you pay for?",
        ],
        "context_vars": {
            "region": NC_REGIONS,
            "ph": ["4.8", "5.2", "5.5", "5.8", "6.0", "6.2", "6.5", "6.8", "7.0", "7.4"],
            "p_index": ["8", "15", "25", "42", "68", "100", "150"],
            "k_index": ["20", "35", "50", "75", "100", "140"],
            "om": ["0.5", "1.2", "2.0", "3.5", "5.0", "8.0"],
            "cec": ["3", "5", "8", "12", "18", "25"],
            "base_sat": ["40", "55", "65", "75", "85", "95"],
        },
    },
    {
        "category": "Soil Science",
        "subcategory": "Clay Soil Improvement",
        "difficulty": "beginner",
        "prompts": [
            "I have terrible {soil_type} in my yard in {region}. How do I make it good for growing vegetables?",
            "Everyone says to add sand to clay soil but I've also heard that's wrong. What should I actually do?",
            "How long does it realistically take to build good garden soil from raw {soil_type}?",
            "Can I grow food directly in {soil_type} or do I need raised beds?",
            "What's the fastest way to improve my {soil_type} for a spring garden?",
        ],
        "context_vars": {
            "soil_type": SOIL_TYPES,
            "region": NC_REGIONS,
        },
    },
    {
        "category": "Soil Science",
        "subcategory": "Composting",
        "difficulty": "beginner",
        "prompts": [
            "What's the best composting method for a small backyard in {region}?",
            "How do I start a compost pile? I have {material_list}.",
            "My compost pile smells terrible. What am I doing wrong?",
            "How long until my compost is ready to use, and how do I know when it's done?",
            "Is vermicomposting worth it? I have a {space} space.",
            "What's the difference between hot composting and cold composting and which is better for me?",
            "Can I compost in {season} in North Carolina?",
            "I want to make compost tea. How do I do it and is it actually worth the effort?",
        ],
        "context_vars": {
            "region": NC_REGIONS,
            "material_list": [
                "kitchen scraps, leaves, and grass clippings",
                "mostly kitchen scraps and cardboard",
                "a ton of fall leaves and some chicken manure",
                "coffee grounds, vegetable scraps, and shredded paper",
            ],
            "space": ["small", "medium", "large"],
            "season": SEASONS,
        },
    },
    {
        "category": "Soil Science",
        "subcategory": "Soil Biology",
        "difficulty": "intermediate",
        "prompts": [
            "I keep hearing about the 'soil food web.' Can you explain what that is and why it matters for my garden?",
            "How do I encourage mycorrhizal fungi in my garden soil?",
            "What kills soil biology, and what am I probably doing wrong?",
            "How do I know if my soil is biologically healthy versus just chemically adequate?",
            "What's the relationship between soil organisms and nutrient availability for my plants?",
            "I've been tilling my garden every spring. Should I stop?",
        ],
        "context_vars": {},
    },
    {
        "category": "Soil Science",
        "subcategory": "Cover Cropping",
        "difficulty": "intermediate",
        "prompts": [
            "What cover crops should I plant this {season} in zone {zone}?",
            "I have empty beds after my {crop} harvest. What can I plant to improve the soil before {next_season}?",
            "How do I terminate a cover crop without tilling?",
            "Can I use cover crops in raised beds or is that just for larger plots?",
            "What's the best cover crop mix for fixing nitrogen in NC?",
            "I want to plant a cover crop that also feeds pollinators. What do you recommend for {season}?",
        ],
        "context_vars": {
            "season": SEASONS,
            "zone": NC_ZONES,
            "crop": ["tomatoes", "summer squash", "corn", "garlic", "spring greens"],
            "next_season": ["spring planting", "fall planting", "next year"],
        },
    },
    {
        "category": "Soil Science",
        "subcategory": "Amendments & Fertility",
        "difficulty": "intermediate",
        "prompts": [
            "My soil test says I need to raise my pH from {ph_low} to {ph_target}. How much lime do I need?",
            "What's the best organic fertilizer program for heavy-feeding crops like tomatoes?",
            "My {crop} leaves are turning {symptom}. What nutrient might be deficient?",
            "When and how should I apply {amendment} to my garden beds?",
            "I want to reduce my dependency on purchased amendments. How do I create a closed-loop fertility system?",
            "What's the difference between feeding the soil and feeding the plant, and why does it matter?",
            "Is biochar worth using? I can get it locally. How do I use it without hurting my soil?",
        ],
        "context_vars": {
            "ph_low": ["4.8", "5.0", "5.2", "5.5"],
            "ph_target": ["6.0", "6.2", "6.5"],
            "crop": COMMON_VEGETABLES[:10],
            "symptom": [
                "yellow between the veins", "purple underneath",
                "brown edges", "pale all over", "yellow from the bottom up",
                "curling and distorted",
            ],
            "amendment": AMENDMENTS,
        },
    },
    {
        "category": "Soil Science",
        "subcategory": "Sheet Mulching & No-Dig",
        "difficulty": "beginner",
        "prompts": [
            "How do I build a new garden bed over grass using sheet mulching?",
            "What's the lasagna gardening method and does it work in NC?",
            "I want to convert part of my lawn to food production without a tiller. Best approach?",
            "How deep should my mulch be, and what's the best mulch for vegetable beds in {region}?",
            "I've heard about the Charles Dowding no-dig method. How would I adapt it for NC's climate?",
            "Can I sheet mulch over Bermuda grass or will it just come back through?",
        ],
        "context_vars": {
            "region": NC_REGIONS,
        },
    },

    # =========================================================================
    # PEST & DISEASE MANAGEMENT
    # =========================================================================
    {
        "category": "Pest Management",
        "subcategory": "Identification & Organic Control",
        "difficulty": "beginner",
        "prompts": [
            "I found {pest} all over my {crop}. What's the most organic way to deal with them?",
            "Something is eating holes in my {crop} leaves but I can't see what. How do I figure out what it is?",
            "How do I prevent {pest} without using chemicals?",
            "My neighbor sprays pesticides on everything. I want to go fully organic. Is that realistic?",
            "What are the most important beneficial insects for a NC garden and how do I attract them?",
            "I found a {mystery_bug} in my garden. Is it a pest or a beneficial?",
        ],
        "context_vars": {
            "pest": COMMON_PESTS,
            "crop": COMMON_VEGETABLES[:12],
            "mystery_bug": [
                "big green caterpillar with a horn",
                "small black and orange beetle",
                "green lacewing-looking thing",
                "fat white grub in the soil",
                "tiny red spider-looking thing on the undersides of leaves",
                "metallic green beetle about half an inch long",
                "wasp-looking thing hovering around my tomatoes",
                "praying mantis",
                "large brown moth at night around my porch light",
            ],
        },
    },
    {
        "category": "Pest Management",
        "subcategory": "Specific Pest Deep Dives",
        "difficulty": "intermediate",
        "prompts": [
            "Squash vine borers destroyed everything last year. How do I prevent them this season in {region}?",
            "I'm losing my battle with Japanese beetles. What actually works long-term?",
            "How do I manage tomato hornworms organically? They keep coming back every year.",
            "Deer are devastating my garden. What are realistic deer management strategies beyond just fencing?",
            "I have a terrible fire ant problem in my garden beds. What's safe to use around food crops?",
            "Root-knot nematodes showed up in my soil test. How do I deal with this without fumigating?",
            "Cabbage worms are destroying all my brassicas. I tried row covers but they still got in.",
            "Cucumber beetles are spreading bacterial wilt in my garden. How do I break the cycle?",
        ],
        "context_vars": {
            "region": NC_REGIONS,
        },
    },
    {
        "category": "Pest Management",
        "subcategory": "Disease Management",
        "difficulty": "intermediate",
        "prompts": [
            "My {crop} has {disease}. What caused it and what can I do now?",
            "How do I prevent fungal diseases in NC's humid summers?",
            "What's the difference between early blight and late blight and does the treatment differ?",
            "My tomato plants wilt during the day but look fine in the morning. What's going on?",
            "I keep getting powdery mildew on my squash every single year. How do I finally beat it?",
            "Is there a way to manage Southern blight organically? I lost half my garden to it last summer.",
            "Blossom end rot keeps hitting my tomatoes. I know it's calcium but I added lime already. What am I missing?",
        ],
        "context_vars": {
            "crop": COMMON_VEGETABLES[:12],
            "disease": COMMON_DISEASES[:10],
        },
    },
    {
        "category": "Pest Management",
        "subcategory": "Integrated Pest Management",
        "difficulty": "advanced",
        "prompts": [
            "I want to set up a real IPM program for my backyard garden. Walk me through how to think about this.",
            "How do I design my garden to naturally reduce pest pressure? I want to stop reacting and start preventing.",
            "What trap crops and companion plants actually work for pest management in NC? I see a lot of claims online.",
            "I want to build habitat for beneficial insects. What should I plant and where?",
            "How do I use crop rotation effectively in a small space to break pest and disease cycles?",
        ],
        "context_vars": {},
    },

    # =========================================================================
    # PLANTING & SPACE OPTIMIZATION
    # =========================================================================
    {
        "category": "Planting",
        "subcategory": "Square Foot Gardening",
        "difficulty": "beginner",
        "prompts": [
            "I have a {bed_size}. How should I lay it out using square foot gardening to grow the most food?",
            "What are the correct spacings for square foot gardening with {crop}?",
            "Is square foot gardening actually more productive than traditional row planting?",
            "I want to grow {crop_list} in a {bed_size}. Can you design the layout for me?",
            "How do I plan succession plantings within a square foot garden framework?",
        ],
        "context_vars": {
            "bed_size": BED_SIZES[:6],
            "crop": COMMON_VEGETABLES[:12],
            "crop_list": [
                "tomatoes, peppers, basil, and lettuce",
                "squash, beans, cucumbers, and herbs",
                "all the salad greens I can fit",
                "root vegetables and cooking greens for fall",
                "a little of everything for a family of four",
            ],
        },
    },
    {
        "category": "Planting",
        "subcategory": "High-Density & Intensive Planting",
        "difficulty": "intermediate",
        "prompts": [
            "How close can I actually plant {crop} before yields start to drop?",
            "I've seen people plant tomatoes 18 inches apart instead of 36. Does high-density tomato planting work in NC's humidity?",
            "What's bio-intensive planting and how does it differ from square foot gardening?",
            "I want to intercrop {crop1} with {crop2}. Will they work together or compete?",
            "How do I do vertical gardening with {crop} to save ground space?",
            "I have only {bed_size} total. Design me the most productive possible garden for a family of four in zone {zone}.",
        ],
        "context_vars": {
            "crop": COMMON_VEGETABLES[:10],
            "crop1": ["tomatoes", "corn", "squash", "peppers", "beans"],
            "crop2": ["basil", "beans", "lettuce", "carrots", "nasturtiums"],
            "bed_size": BED_SIZES[6:],
            "zone": NC_ZONES,
        },
    },
    {
        "category": "Planting",
        "subcategory": "Companion Planting",
        "difficulty": "beginner",
        "prompts": [
            "What are the best companion plants for {crop} in NC?",
            "I've heard you shouldn't plant tomatoes near {crop2}. Is that actually true?",
            "Does the Three Sisters planting (corn, beans, squash) work well in North Carolina?",
            "What flowers should I plant in my vegetable garden and why?",
            "Design me a companion planting layout for a {bed_size} with {crop_list}.",
        ],
        "context_vars": {
            "crop": COMMON_VEGETABLES[:10],
            "crop2": ["potatoes", "fennel", "brassicas", "walnut trees", "peppers"],
            "bed_size": BED_SIZES[:4],
            "crop_list": [
                "tomatoes, peppers, and basil",
                "brassicas and root vegetables",
                "cucurbits and corn",
            ],
        },
    },
    {
        "category": "Planting",
        "subcategory": "Succession Planting & Season Extension",
        "difficulty": "intermediate",
        "prompts": [
            "How do I plan succession planting for {crop} so I have a continuous harvest?",
            "What can I plant after my {crop} finishes in {month} in zone {zone}?",
            "I want three full growing seasons. Walk me through what I plant and when for {region}.",
            "How do I use row covers and cold frames to extend my season in zone {zone}?",
            "What crops can I grow through winter in {region} with minimal protection?",
            "Give me a month-by-month planting calendar for zone {zone}.",
        ],
        "context_vars": {
            "crop": ["lettuce", "beans", "radishes", "carrots", "beets", "tomatoes", "squash"],
            "month": MONTHS,
            "zone": NC_ZONES,
            "region": NC_REGIONS,
        },
    },
    {
        "category": "Planting",
        "subcategory": "Variety Selection",
        "difficulty": "intermediate",
        "prompts": [
            "What are the best {crop} varieties for {region}?",
            "I want disease-resistant tomato varieties that actually taste good. What do you recommend for NC?",
            "What {crop} varieties handle NC's heat and humidity the best?",
            "I want to grow {crop} but I'm in zone {zone} with {soil_type}. Which varieties should I try?",
            "Recommend heirloom vs hybrid varieties for a {experience} in {region}.",
            "What are the best heat-tolerant salad greens for NC summers?",
        ],
        "context_vars": {
            "crop": COMMON_VEGETABLES[:12] + COMMON_FRUITS[:6],
            "region": NC_REGIONS,
            "zone": NC_ZONES,
            "soil_type": SOIL_TYPES,
            "experience": EXPERIENCE_LEVELS,
        },
    },

    # =========================================================================
    # WATER MANAGEMENT
    # =========================================================================
    {
        "category": "Water Management",
        "subcategory": "Watering Decisions",
        "difficulty": "beginner",
        "prompts": [
            "How do I know when my garden needs water? I don't want to over or under water.",
            "How much water do {crop} actually need per week in {season} in NC?",
            "We got {rain_amount} inches of rain this week. Do I still need to water my {crop}?",
            "It's been {temp}F and sunny for {days} days with no rain. My {crop} are in {soil_type}. Should I water?",
            "What time of day should I water and does it really matter?",
            "How do I water newly transplanted seedlings versus established plants?",
        ],
        "context_vars": {
            "crop": COMMON_VEGETABLES[:10],
            "season": ["summer", "late spring", "early fall"],
            "rain_amount": ["0.25", "0.5", "0.75", "1.0", "1.5", "2.0"],
            "temp": ["85", "90", "95", "100", "78", "82"],
            "days": ["3", "5", "7", "10", "14"],
            "soil_type": SOIL_TYPES[:4],
        },
    },
    {
        "category": "Water Management",
        "subcategory": "Irrigation Systems",
        "difficulty": "intermediate",
        "prompts": [
            "Should I use drip irrigation or soaker hoses for my vegetable garden?",
            "I want to set up a simple drip irrigation system for my raised beds. Walk me through it.",
            "How do I set up a rain barrel system? Is one barrel enough for a {bed_size}?",
            "I want to automate my watering with a timer. What settings do I use for {crop} in {season}?",
            "How do I design a water-efficient garden in {region}? We get dry spells in July/August.",
            "What's the most cost-effective irrigation setup for a {experience} with {bed_size}?",
        ],
        "context_vars": {
            "bed_size": BED_SIZES,
            "crop": COMMON_VEGETABLES[:8],
            "season": ["summer", "late spring", "early fall"],
            "region": NC_REGIONS,
            "experience": EXPERIENCE_LEVELS[:2],
        },
    },
    {
        "category": "Water Management",
        "subcategory": "Mulching for Moisture",
        "difficulty": "beginner",
        "prompts": [
            "What's the best mulch for vegetable gardens in NC?",
            "How deep should I mulch and should I mulch right up to the plant stems?",
            "Straw vs wood chips vs leaves — which mulch should I use and when?",
            "Does mulching actually make a big difference in how often I need to water?",
            "I can get free wood chips from a tree service. Are they safe for my vegetable garden?",
        ],
        "context_vars": {},
    },

    # =========================================================================
    # PERMACULTURE & FOOD FORESTS
    # =========================================================================
    {
        "category": "Permaculture",
        "subcategory": "Food Forest Design",
        "difficulty": "advanced",
        "prompts": [
            "I want to start a food forest in my {space} yard in {region}. Where do I begin?",
            "What fruit and nut trees grow well in zone {zone} for a food forest canopy layer?",
            "Design me a fruit tree guild for a {tree} in zone {zone}.",
            "How long until a food forest starts producing meaningful amounts of food?",
            "I have a {slope} slope in my yard. Can I turn it into a productive food forest instead of mowing it?",
            "What are the seven layers of a food forest and what grows in each one in NC?",
        ],
        "context_vars": {
            "space": ["quarter-acre", "half-acre", "small suburban", "large rural"],
            "region": NC_REGIONS,
            "zone": NC_ZONES,
            "tree": ["apple", "peach", "pear", "fig", "persimmon", "pawpaw", "pecan"],
            "slope": ["gentle", "moderate", "steep"],
        },
    },
    {
        "category": "Permaculture",
        "subcategory": "Water Harvesting & Earthworks",
        "difficulty": "advanced",
        "prompts": [
            "How do I build swales to capture rainwater on my {slope} property in {region}?",
            "What's hugelkultur and would it work in NC's climate?",
            "How do I design my yard to capture and infiltrate rainwater instead of it running off?",
            "I have a wet, boggy area in my yard. How can I make it productive instead of a problem?",
            "How do I calculate how much rainwater I can harvest from my roof?",
        ],
        "context_vars": {
            "slope": ["gentle", "moderate", "steep", "mostly flat"],
            "region": NC_REGIONS,
        },
    },
    {
        "category": "Permaculture",
        "subcategory": "Polyculture & Guilds",
        "difficulty": "intermediate",
        "prompts": [
            "What's the difference between a polyculture and just companion planting?",
            "How do I design a self-maintaining perennial polyculture for my NC yard?",
            "I want a low-maintenance edible landscape. What perennial food plants grow well in zone {zone}?",
            "How do I incorporate native plants into my food production system?",
            "What ground covers can I grow between fruit trees that are also edible or nitrogen-fixing?",
        ],
        "context_vars": {
            "zone": NC_ZONES,
        },
    },

    # =========================================================================
    # SEASONAL PLANNING & CALENDARS
    # =========================================================================
    {
        "category": "Seasonal Planning",
        "subcategory": "Monthly Tasks",
        "difficulty": "beginner",
        "prompts": [
            "What should I be doing in my garden right now? It's {month} and I'm in zone {zone}.",
            "I'm a {experience}. What's the most important thing to focus on in {month} in {region}?",
            "Give me a weekly task list for {month} in zone {zone}.",
            "What seeds should I be starting indoors right now? It's {month} in zone {zone}.",
            "I missed my spring planting window. What can I still plant in {month} in zone {zone}?",
            "What should I be harvesting in {month} in {region}?",
        ],
        "context_vars": {
            "month": MONTHS,
            "zone": NC_ZONES,
            "experience": EXPERIENCE_LEVELS,
            "region": NC_REGIONS,
        },
    },
    {
        "category": "Seasonal Planning",
        "subcategory": "Fall & Winter Gardening",
        "difficulty": "intermediate",
        "prompts": [
            "I thought the growing season was over but someone told me I can garden in winter in NC. What can I grow?",
            "When do I need to plant my fall garden in zone {zone}? I feel like I'm already behind.",
            "What's the best overwintering strategy for garlic in {region}?",
            "Can I grow greens all winter in zone {zone} with just row covers?",
            "How do I transition from summer to fall crops without losing any growing time?",
        ],
        "context_vars": {
            "zone": NC_ZONES,
            "region": NC_REGIONS,
        },
    },

    # =========================================================================
    # SEED STARTING & PROPAGATION
    # =========================================================================
    {
        "category": "Propagation",
        "subcategory": "Seed Starting",
        "difficulty": "beginner",
        "prompts": [
            "When should I start {crop} seeds indoors for zone {zone}?",
            "What do I need for a basic indoor seed starting setup? I want to keep costs low.",
            "My seedlings keep getting leggy and falling over. What am I doing wrong?",
            "Should I start {crop} indoors or direct sow in zone {zone}?",
            "How do I harden off seedlings before transplanting outdoors?",
            "What's the best seed starting mix and should I make my own?",
        ],
        "context_vars": {
            "crop": COMMON_VEGETABLES[:12],
            "zone": NC_ZONES,
        },
    },
    {
        "category": "Propagation",
        "subcategory": "Seed Saving",
        "difficulty": "intermediate",
        "prompts": [
            "How do I save seeds from my {crop}? I want to start being more self-sufficient.",
            "What's the easiest vegetable to start saving seeds from as a beginner?",
            "How do I prevent cross-pollination when saving seeds from {crop}?",
            "How long do saved seeds stay viable, and how should I store them?",
            "I want to eventually develop my own locally-adapted varieties. Where do I start?",
        ],
        "context_vars": {
            "crop": ["tomatoes", "peppers", "beans", "lettuce", "squash", "corn"],
        },
    },

    # =========================================================================
    # FRUIT PRODUCTION
    # =========================================================================
    {
        "category": "Fruit Production",
        "subcategory": "Berry Growing",
        "difficulty": "beginner",
        "prompts": [
            "What's the best {fruit} variety for {region}?",
            "How do I grow blueberries in NC? I heard the soil needs to be acidic.",
            "I want to plant a berry patch that produces from spring through fall. What should I plant in zone {zone}?",
            "How do I prune {fruit} and when should I do it in NC?",
            "My {fruit} aren't producing well. What might be wrong?",
        ],
        "context_vars": {
            "fruit": COMMON_FRUITS[:5],
            "region": NC_REGIONS,
            "zone": NC_ZONES,
        },
    },
    {
        "category": "Fruit Production",
        "subcategory": "Tree Fruit",
        "difficulty": "intermediate",
        "prompts": [
            "I want to plant fruit trees in my yard in {region}. What are the best options?",
            "How many chill hours do I get in zone {zone} and which fruit trees will work?",
            "Can I grow {tree} in a small yard using dwarf or semi-dwarf rootstock?",
            "What's the minimum number of fruit trees I need for pollination for {tree}?",
            "How do I organically manage the pests and diseases on {tree} in NC?",
            "Muscadine grapes seem perfect for NC. How do I get started with them?",
        ],
        "context_vars": {
            "region": NC_REGIONS,
            "zone": NC_ZONES,
            "tree": ["peach", "apple", "pear", "fig", "persimmon", "plum", "cherry"],
        },
    },

    # =========================================================================
    # RAISED BEDS & INFRASTRUCTURE
    # =========================================================================
    {
        "category": "Infrastructure",
        "subcategory": "Raised Beds",
        "difficulty": "beginner",
        "prompts": [
            "What's the best way to build raised beds on top of {soil_type}?",
            "How deep do raised beds need to be for {crop}?",
            "What should I fill my raised beds with? I've seen lots of conflicting advice.",
            "Wood vs metal vs concrete block raised beds — which is best for NC?",
            "I'm on a tight budget. What's the cheapest way to get productive raised beds going?",
            "Should I put landscape fabric or cardboard at the bottom of my raised beds?",
        ],
        "context_vars": {
            "soil_type": SOIL_TYPES,
            "crop": ["tomatoes", "root vegetables", "lettuce", "a mix of everything"],
        },
    },
    {
        "category": "Infrastructure",
        "subcategory": "Season Extension Structures",
        "difficulty": "intermediate",
        "prompts": [
            "How do I build a simple cold frame for winter growing in zone {zone}?",
            "What's the difference between a cold frame, low tunnel, and high tunnel?",
            "Is it worth building a greenhouse in {region}? What could I do with it?",
            "How do I build quick hoop/low tunnel covers for my raised beds?",
            "What row cover weight should I use for frost protection vs insect protection?",
        ],
        "context_vars": {
            "zone": NC_ZONES,
            "region": NC_REGIONS,
        },
    },

    # =========================================================================
    # BEGINNER GETTING STARTED
    # =========================================================================
    {
        "category": "Getting Started",
        "subcategory": "First Garden",
        "difficulty": "beginner",
        "prompts": [
            "I've never gardened before. I'm in {region} with a {space} yard. Where do I start?",
            "What are the absolute easiest vegetables to grow in NC for a complete beginner?",
            "I want to start a garden but I'm overwhelmed by all the information. Give me the simplest possible plan.",
            "How much can I realistically grow in {bed_size} as a beginner?",
            "I have kids and want to garden with them. What's fun and easy to grow in NC?",
            "What tools do I absolutely need to get started and what's a waste of money?",
            "I keep killing plants. What am I probably doing wrong?",
            "I rent my house. Can I still garden? I'm in {region}.",
        ],
        "context_vars": {
            "region": NC_REGIONS,
            "space": ["small suburban", "large suburban", "apartment with balcony", "rural"],
            "bed_size": BED_SIZES[:4],
        },
    },

    # =========================================================================
    # AGENTIC / DATA-DRIVEN SCENARIOS
    # =========================================================================
    {
        "category": "Data-Driven Decisions",
        "subcategory": "Sensor Data Interpretation",
        "difficulty": "advanced",
        "prompts": [
            "My soil moisture sensor reads {moisture}% at 6 inches deep. My {crop} are {stage}. It's {month} in zone {zone} and it hasn't rained in {days} days. Should I water?",
            "The temperature is going to drop to {temp}F tonight. I have {crops_out} in the ground. What should I do?",
            "My rain gauge shows {rain} inches this week but my soil moisture sensor says it's dry at 8 inches. What's going on?",
            "Here's my weekly garden data: soil temp {soil_temp}F, air highs {air_high}F, soil moisture {moisture}%, last rain {days} days ago. My {crop} are {stage}. Any actions needed?",
        ],
        "context_vars": {
            "moisture": ["15", "22", "30", "40", "55", "65", "80"],
            "crop": COMMON_VEGETABLES[:8],
            "stage": [
                "just transplanted last week",
                "flowering", "setting fruit", "fruiting heavily",
                "seedlings with 2 true leaves", "mature and almost ready to harvest",
            ],
            "month": MONTHS,
            "zone": NC_ZONES,
            "days": ["2", "3", "5", "7", "10", "14"],
            "temp": ["28", "30", "32", "34", "36"],
            "crops_out": [
                "tomatoes, peppers, and basil",
                "lettuce, kale, and carrots",
                "newly transplanted squash seedlings",
                "strawberries in bloom",
            ],
            "rain": ["0.1", "0.25", "0.5", "1.0", "2.0"],
            "soil_temp": ["45", "55", "65", "75", "85"],
            "air_high": ["70", "78", "85", "92", "98"],
        },
    },
    {
        "category": "Data-Driven Decisions",
        "subcategory": "Planning & Optimization",
        "difficulty": "advanced",
        "prompts": [
            "I have {total_space} of growing space in zone {zone}. I want to maximize caloric production for a family of {family_size}. Design my annual plan.",
            "I tracked my garden expenses last year: ${cost} for a {space} garden. How do I maximize the value of food I produce while cutting costs?",
            "I want to produce at least 50% of my family's vegetables. We're {family_size} people in {region} with {space}. Is this realistic and what would it take?",
            "Here's what I grew last year and the yields: {yield_data}. What should I change this year to be more productive?",
            "I have {hours} hours per week to spend on my garden. How do I get the most food for the least labor?",
        ],
        "context_vars": {
            "total_space": ["200 sq ft", "400 sq ft", "800 sq ft", "1500 sq ft", "quarter acre"],
            "zone": NC_ZONES,
            "family_size": ["2", "4", "5", "6"],
            "cost": ["200", "500", "800", "1500"],
            "space": ["200 sq ft", "500 sq ft", "1000 sq ft"],
            "region": NC_REGIONS,
            "yield_data": [
                "tomatoes: 80 lbs, peppers: 20 lbs, squash: 40 lbs, greens: 15 lbs, beans: 10 lbs",
                "lettuce: 25 lbs, tomatoes: 50 lbs, cucumbers: 30 lbs, herbs: 5 lbs",
            ],
            "hours": ["2", "3", "5", "8", "10"],
        },
    },
    {
        "category": "Data-Driven Decisions",
        "subcategory": "Troubleshooting",
        "difficulty": "intermediate",
        "prompts": [
            "My {crop} plants are {symptom}. Here's what I know: zone {zone}, {soil_type}, planted {when}, last watered {water_info}, last fertilized {fert_info}. What's wrong?",
            "I followed all the advice and my {crop} still failed. I'm in {region}. Can you help me figure out what went wrong?",
            "My soil test says pH {ph}, organic matter {om}%, and my {crop} are showing {symptom}. What's the connection?",
        ],
        "context_vars": {
            "crop": COMMON_VEGETABLES[:10],
            "symptom": [
                "wilting even though the soil is moist",
                "turning yellow from the bottom up",
                "producing flowers but no fruit",
                "growing really slowly compared to my neighbor's",
                "dropping their blossoms",
                "getting brown, crispy leaf edges",
                "producing misshapen fruit",
            ],
            "zone": NC_ZONES,
            "soil_type": SOIL_TYPES[:4],
            "when": ["3 weeks ago", "6 weeks ago", "from seed 8 weeks ago", "last month"],
            "water_info": ["2 days ago, drip irrigation", "daily by hand", "whenever it rains", "3 days ago, soaker hose for 30 min"],
            "fert_info": ["with fish emulsion last week", "never", "with 10-10-10 at planting", "with compost tea monthly"],
            "ph": ["5.0", "5.5", "6.0", "6.5", "7.0", "7.5"],
            "om": ["0.8", "1.5", "2.5", "4.0"],
            "region": NC_REGIONS,
        },
    },

    # =========================================================================
    # ECOSYSTEM & PHILOSOPHY
    # =========================================================================
    {
        "category": "Ecosystem",
        "subcategory": "Pollinators & Beneficials",
        "difficulty": "beginner",
        "prompts": [
            "What should I plant to attract pollinators to my vegetable garden in NC?",
            "How do I attract native bees versus honeybees and does it matter?",
            "I want to build a pollinator garden that also serves my vegetable garden. Design one for zone {zone}.",
            "What native NC wildflowers are best for beneficial insects?",
            "How do I create habitat for ground beetles, spiders, and other predatory insects?",
        ],
        "context_vars": {
            "zone": NC_ZONES,
        },
    },
    {
        "category": "Ecosystem",
        "subcategory": "Philosophy & Motivation",
        "difficulty": "beginner",
        "prompts": [
            "Why should I bother growing my own food when I can buy organic at the store?",
            "I feel guilty about not being more sustainable. Where do I even start?",
            "How do I convince my HOA to let me garden in my front yard?",
            "Is it really possible to feed my family from my backyard?",
            "I want to help my community grow food. How do I start a community garden in NC?",
            "How does backyard gardening actually help the environment? Give me the real numbers.",
            "What's regenerative agriculture and how does it apply at the backyard scale?",
        ],
        "context_vars": {},
    },
    {
        "category": "Ecosystem",
        "subcategory": "Working with Wildlife",
        "difficulty": "intermediate",
        "prompts": [
            "How do I garden alongside deer without just fighting them constantly?",
            "I have a family of {animal} living near my garden. Should I be worried?",
            "How do I balance producing food with being a good steward of the land and wildlife?",
            "What role do toads, lizards, and snakes play in my garden ecosystem?",
            "I found a {animal} nest in my garden bed. What should I do?",
        ],
        "context_vars": {
            "animal": ["rabbits", "chipmunks", "hawks", "black snake", "box turtle", "opossum", "raccoons"],
        },
    },

    # =========================================================================
    # HERBS, MUSHROOMS & SPECIALTY
    # =========================================================================
    {
        "category": "Specialty",
        "subcategory": "Herb Production",
        "difficulty": "beginner",
        "prompts": [
            "What are the best culinary herbs to grow in NC?",
            "I want a kitchen herb garden that produces year-round. What do I plant for zone {zone}?",
            "How do I grow basil in NC without it bolting immediately in summer?",
            "What medicinal herbs grow well in NC?",
            "Can I grow herbs in containers on my porch in {region}?",
        ],
        "context_vars": {
            "zone": NC_ZONES,
            "region": NC_REGIONS,
        },
    },
    {
        "category": "Specialty",
        "subcategory": "Mushroom Cultivation",
        "difficulty": "intermediate",
        "prompts": [
            "Can I grow mushrooms outdoors in NC? What species work best?",
            "How do I inoculate logs for shiitake production?",
            "I want to grow mushrooms in my food forest understory. What's the best approach?",
            "Wine cap mushrooms in garden beds — how does that work and is it worth it?",
        ],
        "context_vars": {},
    },
]


def get_all_topics():
    """Return the full topic list."""
    return TOPICS


def get_topics_by_category(category: str):
    """Filter topics by category name."""
    return [t for t in TOPICS if t["category"] == category]


def get_topics_by_difficulty(difficulty: str):
    """Filter topics by difficulty level."""
    return [t for t in TOPICS if t["difficulty"] == difficulty]
