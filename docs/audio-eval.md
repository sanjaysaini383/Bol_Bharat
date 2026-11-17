# Audio eval: spoken entries under shop noise

Run on 2026-11-17 with `python scripts/eval_audio.py`.

Each case is spoken by Timbre v3.0, mixed with shop noise (overlapping customer
chatter, traffic rumble, a horn), streamed through Prisma v3.0 as a push-to-talk
hold, and scored on the final ledger: exact amount, credit or payment, customer.

STT language: `auto` · format: `transcribe` · LLM fallback: on for the non-Hindi cases

| Condition | Conversations correct | Amounts exact | Direction correct | Wrong line written |
|---|---|---|---|---|
| clean | 24 / 24 | 24 / 24 | 24 / 24 | 0 / 24 |
| 10 dB SNR | 24 / 24 | 24 / 24 | 24 / 24 | 0 / 24 |
| 5 dB SNR | 22 / 24 | 22 / 24 | 22 / 24 | 0 / 24 |
| 0 dB SNR | 7 / 24 | 7 / 24 | 7 / 24 | 1 / 24 |

*Wrong line written* counts conversations that left a line in the ledger which
should not be there. A conversation can fail without one: the agent asked a
question or wrote nothing, which the shopkeeper hears and can retry.

## Failures

- **dedh-sau** at 5 dB SNR: said ['सुनीता ने डेढ़ सौ दिए'], heard ['सुनीता ने 150 रुपये का packet।']; wanted [['Sunita', 'PAYMENT', 150]], got []
- **marathi** at 5 dB SNR: said ['सुनीता ला पाचशे रुपये उधार दिले'], heard ['सुनीताला 500 रुपये उधार दिले.']; wanted [['Sunita', 'CREDIT', 500]], got []
- **ps-sentence** at 0 dB SNR: said ['सुरेश का तीन सौ चालीस उधार, कल दो सौ दिया था'], heard ['सुरेश का 340, और कल 200 दिया।']; wanted [['Suresh', 'CREDIT', 340], ['Suresh', 'PAYMENT', 200]], got []
- **three-forty** at 0 dB SNR: said ['Suresh ka three forty udhaar likh do'], heard ['सुरेश का 340, रमेश गुप्ता।']; wanted [['Suresh', 'CREDIT', 340]], got []
- **dedh-sau** at 0 dB SNR: said ['सुनीता ने डेढ़ सौ दिए'], heard ['तो बाकी हम लोग, तो पहले biscuit का packet।']; wanted [['Sunita', 'PAYMENT', 150]], got []
- **pachhattar** at 0 dB SNR: said ['सुनीता का पचहत्तर रुपये उधार'], heard ['सुनीता का 75 रुपए।']; wanted [['Sunita', 'CREDIT', 75]], got []
- **hazaar-250** at 0 dB SNR: said ['सुरेश का एक हज़ार दो सौ पचास उधार'], heard ['सुरेश का khata 1250 का है।']; wanted [['Suresh', 'CREDIT', 1250]], got []
- **english-paid** at 0 dB SNR: said ['Suresh paid two hundred rupees'], heard ['और अगर सुरेश paid to the khata?']; wanted [['Suresh', 'PAYMENT', 200]], got []
- **english-took** at 0 dB SNR: said ['Sunita took goods worth five hundred and fifty'], heard ['सुनीता, तुम खुद से बात करो। भाई, तुम भाई हैं, रमेश गुप्ता।']; wanted [['Sunita', 'CREDIT', 550]], got []
- **fix-amount** at 0 dB SNR: said ['सुरेश का तीन सौ चालीस उधार', 'नहीं, चार सौ तीस'], heard ['सुरेश का 340 रुपये का khata।', 'नहीं देगा 430।']; wanted [['Suresh', 'CREDIT', 430]], got []
- **fix-direction** at 0 dB SNR: said ['सुनीता का दो सौ उधार', 'नहीं, जमा था'], heard ["सुनीता का latest show, 'खाता मालिक'।", 'नहीं, कल नहीं। वो सामान जमा है, ये biscuit।']; wanted [['Sunita', 'PAYMENT', 200]], got []
- **sadsath** at 0 dB SNR: said ['सुनीता का सड़सठ रुपये उधार'], heard ['सुनीता का 67 रुपए का khata।']; wanted [['Sunita', 'CREDIT', 67]], got []
- **navaasi** at 0 dB SNR: said ['सुरेश ने नवासी रुपये दिए'], heard ['सुरेश ने 89।']; wanted [['Suresh', 'PAYMENT', 89]], got []
- **do-hazaar-500** at 0 dB SNR: said ['सुनीता का दो हज़ार पाँच सौ उधार'], heard ['ये सुनीता madam के लिए।']; wanted [['Sunita', 'CREDIT', 2500]], got []
- **descriptor** at 0 dB SNR: said ['दूध वाले रमेश का एक सौ बीस उधार'], heard ['दूध वाले रमेश गुप्ता, एक family है और आज।']; wanted [['Ramesh Kumar', 'CREDIT', 120]], got []
- **shared-name** at 0 dB SNR: said ['रमेश का पाँच सौ उधार', 'गुप्ता'], heard ['रमेश गुप्ता, ये biscuit कम खाएगा।', 'रमेश गुप्ता, ये भी सही है।']; wanted [['Ramesh Gupta', 'CREDIT', 500]], got []
- **new-customer** at 0 dB SNR: said ['मोहन का अस्सी रुपये उधार'], heard ['ये biscuit का khata है, ये भी लिख देना।']; wanted [['Mohan', 'CREDIT', 80]], got []
- **sava-do-sau** at 0 dB SNR: said ['सुरेश का सवा दो सौ उधार'], heard ['सुरेश का सवाल, "रूस को udhaar?"']; wanted [['Suresh', 'CREDIT', 225]], got []
- **kannada** at 0 dB SNR: said ['ಸುರೇಶ್ ಗೆ ಮುನ್ನೂರು ರೂಪಾಯಿ ಸಾಲ ಬರೆಯಿರಿ'], heard ['तो सुरेश के 150 रुपये हिसाब का payment किया।']; wanted [['Suresh', 'CREDIT', 300]], got [['Suresh', 'PAYMENT', 150]]

## What this does not measure

The speech is synthetic and comes from the same vendor as the recogniser, so real
voices, accents and phone microphones will score lower. The noise is generated, not
recorded in a shop. Treat the table as a regression check on number formats,
code-mix and noise handling, not as field accuracy.
