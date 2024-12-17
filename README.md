# Bazarr_AutoTranslate
Script to automatically translate subtitles with Bazarr via cron

I agree with the original creator, that it is hard to find subtitles for some old/unpopular series/movies, genres or even animes and in most of the cases they have English subtitles embedded or at least online.
I Updated this script because auto-translation did not work for me using the original script by anast20sm

The script first tries to download subtitles in a ENG and then it translates to your desired language. Languages can be modified in `FIRST_LANG` and `SECOND_LANG`(if only want one, leave empty).

### Required
- Bazarr (obviously)

## Best Bazarr settigns to optimize this script:
- Configure **Embedded Subtitles** Provider


![imagen](https://github.com/anast20sm/Bazarr_AutoTranslate/assets/33606434/d5e5b443-b0ae-4adb-b32b-07a6f5338a1d)


- Disable **Use Embedded Subtitles**


![imagen](https://github.com/anast20sm/Bazarr_AutoTranslate/assets/33606434/e2712537-1e83-4590-9cc4-1f2e47ad0cbc)


Embedded subtitles cannot be used/modified by Bazarr so with these two settings it will extract the embedded subtitles in case there are (by default I only programmed extract English subs).

- Enable **Upgrade Previously Downloaded Subtitles** and **Upgrade Manually Downloaded or Translated Subtitles**


 ![imagen](https://github.com/anast20sm/Bazarr_AutoTranslate/assets/33606434/42736f20-fb55-43de-b45e-a07cceea73d2)


 
 ![imagen](https://github.com/anast20sm/Bazarr_AutoTranslate/assets/33606434/5c1eb5c1-e52f-42c4-a871-eb4cfbb90582)


This is recommended to always have the best possible subtitles, and if possible one made by a person who understands what is happening in the show/movie and writes with context.
I repeat again, as translated subtitles will never be as good as subtitles made by someone, this setting will ensure translated is only the last option.

Note: Code assumses that you have 3 languages set in profile, english being one of them:

![{F7585D44-4699-46F1-94FC-81FC559FF036}](https://github.com/user-attachments/assets/e346d80a-e295-4cf2-aaa7-e26c46bd08d3)
