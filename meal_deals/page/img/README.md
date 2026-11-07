# Meal photos

Photos of similar dishes shown on the Dinner Deals page. They are not RecipeTin Eats
photos: her site opts out of Anthropic's crawlers and her images are her own work.

Each photo was found through [Openverse](https://openverse.org) (Creative Commons search),
licensed CC BY 2.0 or CC BY-SA 2.0, then cropped to 3:2 and resized to 900×600.
`credits.json` records the photographer, licence and source page for every file; the page
shows the credit on each meal and lists them all under "Photo credits".

To add a dish: add a JPEG here named `<key>.jpg`, add its entry to `credits.json`, copy the
entry into the `PHOTOS` table in `dinner_deals.html`, and add a name rule to `PHOTO_RULES`.
