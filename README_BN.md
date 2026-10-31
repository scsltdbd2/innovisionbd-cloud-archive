# InnovisionBD — GitHub Actions Archive Worker (Cloud Console ছাড়া)

এটি **আগের V12/V13/V14 Hostinger PHP `cloud_gateway.php`** ব্যবহার করে Drive Archive চালায়। Cloud Run, Google Cloud billing, Python install অথবা SSH/CLI প্রয়োজন নেই। Browser বন্ধ হলেও GitHub Actions থেকে কাজ হবে, যদি scheduled workflow সক্রিয় ও Source/Destination Account অনুমোদিত থাকে।

## কোথায় কোন ফাইল রাখবেন

- **GitHub private repository root:** `worker.py`, `run_github.py`, `requirements.txt`।
- **GitHub repository:** `.github/workflows/archive.yml` (এই ঠিক Path-এ)।
- **Hostinger `public_html/business-data/`:** বিদ্যমান `cloud_gateway.php`, `index.php`, `app/` এবং `storage/config.php` অক্ষত রাখুন। এই ZIP-এর Python ফাইল Hostinger-এ Upload করতে হবে না।

## Setup: কোনো CLI ছাড়াই

1. `https://innovisionbd.com/business-data/?page=cloud`-এ Login করে **Gateway Secret** তৈরি/Copy করুন। `IVBD_GATEWAY_SECRET` হিসেবে ব্যবহৃত Secret-টি **64-character hexadecimal** হওয়া চাই। আগের Secret পুনরায় Generate করলে পুরোনো Worker আর কাজ করবে না।
2. `https://github.com/new` থেকে **Private** repository তৈরি করুন, যেমন `innovisionbd-drive-archive`।
3. ZIP Extract করুন। GitHub-এর **Add file → Upload files**-এ পুরো Extract করা Folder-এর ভেতরের ফাইল ও `.github` Folder drag/drop করুন। Upload-এর পর **Actions** tab-এ Workflow দেখা যাচ্ছে কি না পরীক্ষা করুন; `.github/workflows/archive.yml` সঠিক অবস্থানে থাকা জরুরি। Hidden Folder drag/drop সমস্যা হলে GitHub Web Editor-এ **Create new file** দিয়ে `.github/workflows/archive.yml` Path-এ YAML Content Paste করুন।
4. Repository **Settings → Secrets and variables → Actions → New repository secret**: `IVBD_GATEWAY_SECRET` নামে Secret যোগ করুন; Value হবে Website-এর Gateway Secret। GitHub Actions-এর Log-এ Secret প্রকাশ করবেন না।
5. Repository **Actions → InnovisionBD Drive Archive → Run workflow → Run workflow** ক্লিক করুন। প্রথম Run-এর `Process Drive archive queue` ধাপের ফলাফল দেখুন।
6. Success হলে **Actions schedule** স্বয়ংক্রিয়ভাবে প্রায় **৫ মিনিট অন্তর** কাজ চালাবে। GitHub schedule সময়মতো Run হওয়ার নিশ্চয়তা নেই; কখনো Delay বা Drop হতে পারে। নিরবচ্ছিন্ন ২৪/৭ Server Guarantee হিসেবে এটি ব্যবহার করবেন না।
7. Website-এ Main Workspace Destination ও Source Gmail Authorize করে **Start Full Archive** দিন। Worker প্রতিবার Queue Process করবে এবং Progress MySQL-এ সংরক্ষিত থাকবে।

## প্রযুক্তিগত সীমা ও সতর্কতা

- GitHub Actions scheduled jobs শুধু repository-এর **default branch**-এর Workflow চালায়; Workflow File সেখানেই Commit থাকতে হবে।
- Archive Buffer GitHub-hosted runner-এ অস্থায়ী; প্রতিবার নতুন Runner হলেও Hosting Database-এর Checkpoint থেকে কাজ পুনরায় শুরু হয়।
- বর্তমানে Worker 4 concurrent transfers দিয়ে শুরু হয়; Hosting Rate Limits, Google API quotas এবং GitHub-hosted runners' bandwidth/usage limits প্রযোজ্য। বৃহৎ বা দীর্ঘস্থায়ী Backup-এর জন্য GitHub Actions **production archive infrastructure-এর আদর্শ বিকল্প নয়**।
- Worker source-to-destination transfer করে। Google-native Docs/Sheets/Slides উপযুক্ত Office Format-এ Export হয়। Unsupported File/Google exports সীমাবদ্ধতা থাকলে Database-এ Failure/Partial Report থাকবে।
- GitHub Private Repository হলেও Third-party Actions Runner-এ ক্ষণস্থায়ী Google Access Token ও File Contents Process হবে। Secret, Source Data, Log ও Repository Access সতর্কতার সঙ্গে নিয়ন্ত্রণ করুন।
- এই Package **Google OAuth-এর `access_not_configured` Error সমাধান করে না**। Source ও Workspace OAuth আগে সফল হতে হবে এবং Hostinger Gateway কার্যকর হতে হবে।
- `Cloud worker connection Not detected` status Run শুরু হওয়ার আগে স্বাভাবিক। GitHub থেকে `check` Request সফল হলে Hostinger-এ Last Authenticated Gateway Request Update হওয়ার কথা; পুরোনো Cloud Run URL-এর Test Form GitHub Worker-এর জন্য নয়।
- Package Google Cloud Console ছাড়া একটি কার্যকর **alternative runner implementation**, কিন্তু User Account-এ Live Authorize, Repository Setup বা Online End-to-End Transfer এখনো পরীক্ষা করা হয়নি।

## Troubleshooting

`Gateway check failed` → Website-এর `cloud_gateway.php` সঠিক আছে কি না এবং Secret একই কি না যাচাই করুন।
`Workspace destination is not connected` → Website → Workspace setup → Main Destination Authorize করুন।
`403 Forbidden` → Gateway Secret mismatch, `X-Archive-Secret` header filtering, অথবা Hostinger Security Rules দেখুন।
`Archive progress 0` → Website-এ Start Full Archive করা হয়েছে কি না, Source Gmail authorize আছে কি না, Hosting PHP Error Log এবং GitHub Actions job log দেখুন।
