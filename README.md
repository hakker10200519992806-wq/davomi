# 🌟 LangLearn Studio

Til o'rgatish uchun interaktiv darslik platformasi.
Red Kalinka uslubida dizayn, SQLite ma'lumotlar bazasi.

## 🚀 Ishga tushirish

```bash
python run.py
```

Brauzer avtomatik ochiladi: http://127.0.0.1:5000

## 📁 Loyiha tuzilmasi

```
langlearn/
├── app.py              ← Flask backend + API + SQLAlchemy
├── run.py              ← Ishga tushirish skripti
├── data/
│   └── langlearn.db    ← SQLite ma'lumotlar bazasi
├── static/
│   ├── audio/          ← Yuklangan audio fayllar
│   └── img/            ← Yuklangan rasmlar
└── templates/
    └── index.html      ← Asosiy frontend (HTML+CSS+JS)
```

## 🧩 Blok turlari

| Blok | Tavsif |
|------|--------|
| 🔤 Sarlavha | H1-H4, rang sozlanadi |
| 📖 So'zlar (Vocab) | Rus + tarjima + audio |
| 💬 Dialog | A/B ko'rinishida dialog |
| ✏️ Bo'sh joy | Fill-in-the-blank mashqi |
| 🔗 Moslashtirish | Juftlikni topish o'yini |
| 🧠 Test (Quiz) | Ko'p tanlovli savollar |
| 🔲 Krossvord | Interaktiv krossvord |
| 🖼️ Rasm | Rasm yuklash + o'lcham |
| 🔊 Matn+Ovoz | Audio bilan o'qiladigan matn |
| 📊 Jadval | Grammatika jadvallari |
| ― Chiziq | Ajratuvchi chiziq |

## 💾 Ma'lumotlar

Barcha darslar va bloklar `data/langlearn.db` faylida saqlanadi.
Dastur o'chirilsa ham ma'lumotlar yo'qolmaydi.

## 🌍 Tillar

Istalgan til juftligi uchun moslashtirilishi mumkin:
- Rus → O'zbek
- Ingliz → O'zbek  
- Nemis → O'zbek
- va boshqalar...
