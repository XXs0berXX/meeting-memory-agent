# Eval results (11 hand-written questions)

- **Judge score: 9.5 / 11** (correct = 1, partial = 0.5)
- Key-fact coverage (regex, no LLM): 88%
- Citations verified word-for-word: 32/32
- Mean agent steps: 2.1, mean latency: 19s per question

| id | team | verdict | facts | cites ok | judge note |
|---|---|---|---|---|---|
| E01 | ES2013 | correct | 3/3 | 3/3 | Nothing material. Candidate covers batteries (including rechargeable), solar, and kinetic, says kinetic was the third-meeting choice and sti |
| E02 | IS1006 | correct | 4/4 | 3/3 | Nothing significant. The candidate describes the style as 'universal' rather than 'good', and it does not name Speaker D as the one who like |
| E03 | ES2015 | correct | 3/4 | 3/3 | Nothing wrong on the key facts: kinetic energy, solar backup dropped, and a cost of three Euros all match. The extra details (no battery, th |
| E04 | IS1004 | correct | 5/5 | 3/3 | Nothing material. Speech recognition ruled out first in meeting b, LCD dropped in meeting c, with cost (price target) and Speaker D's intern |
| E05 | IS1004 | correct | 5/5 | 3/3 | Nothing material. The candidate matches the reference: rechargeable batteries with a base station plus solar cells proposed by the Industria |
| E06 | ES2005 | partial | 3/4 | 3/3 | The candidate is right that speech recognition stayed and the final design has no screen. But it says the screen was dropped in the second m |
| E07 | IS1000 | correct | 3/3 | 3/3 | Nothing missing. Limit 12.50, advanced design 16 euros, speech recognition about 3 euros all match the reference. The extra claim that the t |
| E08 | IS1001 | correct | 3/3 | 2/2 | Nothing missing or wrong. The candidate matches the reference: standard battery and odd banana-shaped spongy case in meeting 3, and both kep |
| E09 | TS3012 | partial | 2/4 | 3/3 | Core outcome is right: speech recognition was cut and the LCD kept. But the reason differs from the reference. The reference says the cut wa |
| E10 | TS3008 | correct | 3/3 | 3/3 | Nothing material. The candidate matches the reference: C preferred a basic battery, A pushed for kinetic power in the final meeting, and the |
| E11 | ES2012 | partial | 2/3 | 3/3 | Candidate confirms voice recognition is in the final design and mentions the microphone for voice commands, but omits the spoken 'where are  |

## Answers

### E01 (correct)
**Q:** Team ES2013 weighed several energy sources for their remote. Which options were discussed, which one did they settle on in their third meeting, and was it still the choice in the final costing?

**Gold:** Batteries (including the idea of a rechargeable internal battery), solar power and kinetic energy were discussed. In meeting c the consensus was kinetic energy, if at all possible, and in meeting d the final costing still used a single kinetic energy source.

**Agent:** They discussed a standard battery (including a rechargeable one), kinetic, wind-up/hand dynamo, and solar. Solar was dropped because it doesn't work in the dark. In the third meeting they settled on kinetic energy, and in the final costing spreadsheet it was still the choice: kinetic was the single energy component, with only one needed.

- `ES2013c`: "the choices that we've discussed were pretty much battery, kinetic or solar"
- `ES2013d`: "Um we would use plastic with a rubber casing, I think was the consensus, powered by kinetic energy."
- `ES2013d`: "Kinetic is um and how many of those will we need per we only need one."

### E02 (correct)
**Q:** In Team IS1006, who first proposed making the remote out of titanium and why, what was said about rubber as an alternative, and which material did the final design keep?

**Gold:** Speaker B proposed titanium in meeting b, for a baby-proof, unbreakable remote with a good style. In meeting c rubber was weighed (Speaker D liked its spongy feel; Speaker C said rubber is expensive and hard to shape), but in meeting d they stuck with titanium.

**Agent:** Speaker B (User Interface Designer) first proposed titanium in the second meeting, saying it would make the remote baby-proof, unbreakable and universal in style. In the third meeting Speaker C (Industrial Designer) said rubber is expensive and makes it difficult to mould the shapes and buttons, although it was later suggested as a softer, spongy alternative. The final design kept titanium as the casing material.

- `IS1006b`: "For personal preferences I think that to make a baby-proof remote control it got to be a titanium."
- `IS1006c`: "We'll use mainly for titanium rubber is expensive and also it's bit difficult to do all the shapes uh."
- `IS1006d`: "But we still didn't want to go far from our titanium idea"

### E03 (correct)
**Q:** What energy source did Team ES2015 choose, what happened to the idea of a solar backup, and what did the energy source cost in their final costing?

**Gold:** They chose kinetic energy (Speaker B, the Industrial Designer, favoured it in meeting c). A solar backup was suggested in meeting c but dropped in favour of going fully kinetic. In meeting d the kinetic source was costed at three Euros, described as quite a large expense.

**Agent:** The team chose a kinetic energy source and decided to use it alone, with no battery backup. The solar backup idea, floated as a replacement for a battery backup, was never adopted; Speaker B (Industrial Designer) later said the LCD screen and solar power backup would have been unaffordable. In the final costing, kinetic energy cost three Euros, as part of a total operating cost of twelve twenty Euros.

- `ES2015c`: "It could be fully kinetic energy. Yeah."
- `ES2015d`: "if we had decided to use the L_C_D_ screen, and like solar power backup and everything, then we wouldn't have been able to afford that."
- `ES2015d`: "Okay we're using kinetic which is quite a large expense at three Euros."

### E04 (correct)
**Q:** Team IS1004 considered both an LCD screen and speech recognition. Which was ruled out first, in which meeting was the other ruled out, and what arguments were made against the LCD?

**Gold:** Speech recognition was ruled out first, in meeting b, while the LCD was left open. The LCD was dropped in meeting c, because of cost against the 25 Euro price target and because Speaker D (Marketing Expert) argued it needed an internet connection and more requirements.

**Agent:** Speech recognition was ruled out first, in the second meeting (IS1004b), when Speaker A (Project Manager) said the team would not go for it and left the LCD open. The LCD was ruled out in the third meeting (IS1004c), which the final meeting's recap confirms. The arguments against the LCD were mainly cost and extra requirements: Speaker D (Marketing Expert) said it needs an internet connection and more things to buy, and Speaker A (Project Manager) said earlier that the target price could not cover both an LCD and speech recognition.

- `IS1004b`: "we don't go for speech recognition technology. The L_C_D_ is still on disc is still open to discussion."
- `IS1004c`: "I'm not convinced about this L_C_D_ because you need uh internet connection, you need more things, it's not just buying a new control re remote"
- `IS1004d`: "No L_C_D_, no speech recognition technology, okay"

### E05 (correct)
**Q:** What power source did Team IS1004's industrial designer propose, and what happened to the solar cells in the final meeting and why?

**Gold:** Speaker C (Industrial Designer) proposed rechargeable batteries with a base (docking) station plus solar cells. In meeting d Speaker B (User Interface Designer) argued solar cells were no longer necessary with a recharging base station, and the team decided against them.

**Agent:** Speaker C (Industrial Designer) proposed two power sources: ordinary batteries, rechargeable on a base station, plus solar cells for use in good lighting. In the final meeting the solar cells were dropped and the team stuck with a single rechargeable battery, because Speaker B (User Interface Designer) argued they were no longer necessary once there was a recharging base station, and Speaker C agreed that rechargeable batteries now covered the power need.

- `IS1004c`: "on top of that we can have solar cells, when the lighting conditions are good they can be used"
- `IS1004d`: "I don't really know if the solar cells are actually necessary any more if you have a recharging base station."
- `IS1004d`: "Okay so we we stick to battery, one."

### E06 (partial)
**Q:** Did Team ES2005 keep speech recognition in their final design, and what happened to the screen idea and why?

**Gold:** Yes: speech recognition was voted in during meeting b and the final design in meeting d is mainly speech-operated, with buttons on the flip side. The LCD screen was dropped: in meeting c Speaker D argued it was too expensive for the 12.50 Euro budget and not right for an easy-to-use product, so the final design has no screen.

**Agent:** Yes. Speech recognition stayed in the final design: in the last meeting Speaker D (User Interface Designer) presented a prototype with as few buttons as possible that relies mainly on speech recognition. The screen idea was dropped in the second meeting. Speaker A (Industrial Designer) said a TFT screen is fragile and could break, and Speaker A also said speech recognition is much cheaper, as it needs only a microphone and some integrated circuits. When Speaker C (Project Manager) called a vote, nobody wanted the TFT. The team later kept the LCD screen out, and the final prototype has no screen.

- `ES2005b`: "there's a good chance the the T_F_T_ screen would break or uh get damaged. They're pretty fragile."
- `ES2005b`: "Who wants T_F_T_? No-one does. Excellent, so we'll go with speech recognition, yeah?"
- `ES2005d`: "you can flip it over, and there's some there, um but mainly it's speech recognition."

### E07 (correct)
**Q:** What production cost limit was Team IS1000 working to, what did their more advanced design with speech recognition cost in the final meeting, and how much of that was the speech recognition?

**Gold:** The limit was 12.50 Euros per unit (stated in meeting a and repeated in meeting d as non-negotiable). The advanced design came to 16 Euros, out of budget, of which the speech recognition system was about 3 Euros (about 13 without it); the basic version was about 8 Euros.

**Agent:** Team IS1000 was working to a production cost limit of 12.50 euros. In the final meeting, the more advanced design (with speech recognition) was costed at sixteen euros, of which about three euros was the speech recognition, which would bring it to thirteen euros without it. The team then dropped speech recognition and went with the basic design at about twelve euros.

- `IS1000c`: "I'm not sure whether that that we can implement that for twelve Euro and fifty cents."
- `IS1000d`: "Then it's out of budget. But the the main point we we talk about that with our uh manufacturer."
- `IS1000d`: "So that takes it down to thirteen Euros without the speech recognition."

### E08 (correct)
**Q:** What power source and case did Team IS1001 agree on in their third meeting, and were these kept in the final meeting?

**Gold:** In meeting c they agreed on a standard battery and an odd (banana) shape with a spongy cover. Meeting d kept both: a standard battery and a yellow banana-shaped case with a rubber material around it.

**Agent:** In the third meeting the team agreed on a standard battery and an odd, banana-shaped case with a spongy cover. In the final meeting, Speaker A (Project Manager) recapped the same standard battery and a yellow banana-shaped case with rubber around it for a spongy feel, so both were kept.

- `IS1001c`: "Okay odd shape with spongy cover. And standard battery okay, a chip imprint, there's no specific problem."
- `IS1001d`: "to have a standard battery, to have a yellow banana shaped uh case with uh a rubber material around it"

### E09 (partial)
**Q:** Team TS3012 had agreed to put both an LCD screen and speech recognition in their remote. What happened to these in their final meeting, and what reason was given?

**Gold:** Changed finances forced a cut in meeting d: Speaker A (Project Manager) proposed taking out speech recognition and keeping the LCD, because about ninety percent of users favoured an LCD display but only sixty percent favoured speech recognition.

**Agent:** In the final meeting, speech recognition was left out of the base product. Customers who want it would have to pay more, because Speaker C (Industrial Designer) said it cannot be done at this price. The LCD screen was kept, because Speaker A (Project Manager) judged it worth more than speech recognition and said it was not affordable to have both.

- `TS3012d`: "We agreed that we would implement both the L_C_D_ and speech recognition"
- `TS3012d`: "then they must be prepared to pu to pay more, because it's cannot be done for this."
- `TS3012d`: "I think the L_C_D_ screen is more worth than speech recognition."

### E10 (correct)
**Q:** When Team TS3008 reviewed energy sources, which option was preferred, and did the push for kinetic power in their final meeting change the design?

**Gold:** Speaker C preferred a basic battery when presenting the options in meeting c. In meeting d Speaker A pushed for a kinetic part as a special feature, but it did not make it in: the design stayed with one battery, and A said they would have liked a kinetic part as well.

**Agent:** Speaker C (Industrial Designer) preferred a basic battery over kinetic or solar power, because a remote can sit on a table for a day and doesn't need to be carried around. In the final meeting, Speaker A (Project Manager) pushed for kinetic power, but Speaker D (Marketing Expert) and Speaker C objected. A then conceded and said they would leave it as is, saying they would have liked a kinetic part. The transcript shows no sign that kinetic power was built into the design.

- `TS3008c`: "a remote control can lie on a table for a day, and then you push uh a button"
- `TS3008d`: "Okay, then we d Okay, well y we don't have to do it"
- `TS3008d`: "I would have liked a kinetic part as well, to give it some just to give it some extra special feature"

### E11 (partial)
**Q:** Did voice recognition make it into Team ES2012's final remote, and what voice features did the final design have?

**Gold:** Yes. Voice recognition, pushed by Speaker A from meeting b and confirmed in meeting c, is in the final design in meeting d: a microphone for voice commands, plus a spoken 'where are you' function to find the remote.

**Agent:** Yes, voice recognition made it into the final design. Speaker A (Project Manager) said in the last meeting that the expensive voice sensor was a basic, fundamental decision that had to stay, and Speaker B (Industrial Designer) presented a prototype with a microphone for voice commands alongside a volume scroll, channel up/down buttons, a power key and a menu key. So the final remote combined voice commands with manual controls.

- `ES2012c`: "I'm I'm incli kinda inclined to say that we should just go for it."
- `ES2012d`: "Uh we got a microphone there which for all the voice commands so you can you know talk to it like that and it'll still understand."
- `ES2012d`: "The uh the the voice sensor is is expensive but we we made a a basic decision that that was absolutely fundamental"
