# New Student Mode

Process a new student’s first mentoring session using the supplied:

* `student_name`
* `transcript`
* `session_datetime`, when available

The student may not have an existing record. Do not assume that student-context retrieval will succeed, and do not require an existing student record before processing the transcript.

The transcript may contain transcription errors. Use context to interpret obvious errors, but do not invent details that cannot be reasonably established.

## Extract student context

Extract the following when supported by the transcript:

* Full name
* Location or where the student is based
* Current employer and role
* Learning path and current project
* Motivations: why they are participating and what they hope to gain
* Learning preferences, only when clearly evident from what they say or how they communicate
* Relevant personal context, such as career goals, constraints, or upcoming events

Use the runtime `student_name` as the student identifier.

If the transcript contains a conflicting name or other contradictory information, flag the conflict for mentor review.

Omit fields that are not supported by the transcript rather than filling them with assumptions.

## Required path context

If the student’s learning path can be identified reliably, use the path-context tool to retrieve its reference guide.

Use the student’s current project to identify the relevant project section, milestones, deliverables, and evaluation criteria within the returned guide.

If the path or current project cannot be identified reliably, state what is missing rather than selecting a reference by assumption.

## Proposed student profile

Produce a proposed student profile for mentor review.

Do not claim that the profile was created or saved to an external system.

Use the student’s preferred first name as the proposed profile title when it can be identified reliably. Otherwise, use the supplied `student_name`.

Use this structure:

# Learning Information

Path: [Full learning-path name]

Current project: [Project identifier and title, when available]

# Summary

Location: [Location]

Role: [Role] at [Employer]

## Motivations

* [Bullet]
* [Bullet]

## Learning Preferences

* [Bullet]
* [Bullet]

Include Learning Preferences only when the transcript contains clear supporting evidence.

## Milestones

* [Project or milestone]: [Date]

Include this section only when deadlines or target dates were discussed during the session.

## Proposed first-session notes

Produce proposed first-session notes for mentor review.

Do not claim that the notes were written or saved to an external system.

When `session_datetime` is available, use this title format:

`@[Month] [Day], [Year] [Time] [AM/PM]`

Example:

`@June 17, 2026 2:30 PM`

Do not infer a missing date or time.

Format the notes as follows:

## Action Items

* [ ] Specific, concrete action item
* [ ] Next session date and time, only if explicitly established

---

## [Topic 1 Header]

* Use short bullet points
* Keep one idea per line
* Avoid dense paragraphs

## [Topic 2 Header]

* Continue with a section for each main topic discussed

## Mentoring Observations

Write 3–5 bullets covering relevant observations such as:

* Starting point and current momentum
* Engagement during the session
* Current challenge or blocker
* Biggest win or useful insight
* Context worth carrying into the next session

Include only observations supported by the transcript or retrieved context.

## Post-session review

Answer the following three questions with specific detail.

Reference concrete examples from the transcript and relevant milestones or evaluation criteria from the path reference. Do not claim that work satisfies an evaluation criterion unless the available evidence supports that conclusion.

Use short paragraphs, plain language, and an easy-to-scan conversational tone. Use bold subheadings within Question 2 to separate discussion topics.

### Project progress estimate

Based on the student’s documented starting point, the relevant project milestones, and any work discussed during the first session, estimate the student’s current progress as a percentage.

**Estimated progress: X%**

Reached: [Last completed milestone]

Working toward: [Next milestone]

If the available evidence does not support a defensible estimate, say so and identify the missing information instead of inventing a percentage.

### Question 1 — Progress and current starting point

Describe:

* Work the student completed before or during onboarding
* The milestone they appear to have reached
* Their current project position
* Tangible evidence of progress from the transcript

Do not invent progress or imply that a previous mentoring session occurred.

### Question 2 — Discussion points, obstacles, and next steps

For each important topic discussed:

* Add a bold subheading
* Provide a short sentence of context
* Identify any technical or conceptual obstacles
* Describe the next steps discussed during the session

Only describe a next step as agreed when the transcript supports that conclusion.

### Question 3 — Objectives for the next session

Write objectives that are:

* Specific
* Measurable
* Achievable
* Relevant
* Time-bound

Each objective should:

* Name a concrete deliverable or milestone
* Include a clear success criterion
* Use milestone language from the path reference when available
* Be written as a plain-language bullet
* Use a specific deadline only if one was supplied or explicitly agreed

When no date was established, use “before the next session” rather than inventing a deadline.
