from datetime import datetime, timedelta
from pathlib import Path
from sys import exit as sys_exit
from typing import Any, Callable, Iterable, Optional

from tinydb import Query, where
from tmdbapis import TMDbAPIs, NotFound, Unauthorized, TMDbException
from tmdbapis.objs.reload import Episode as TMDbEpisode
from tmdbapis.objs.image import Still as TMDbStill

from modules import global_objects
from modules.Debug import log
from modules.EpisodeDataSource import EpisodeDataSource
from modules.EpisodeInfo import EpisodeInfo
from modules.PersistentDatabase import PersistentDatabase
from modules.SeriesInfo import SeriesInfo
from modules.WebInterface import WebInterface


def catch_and_log(
        message: str,
        log_func: Callable[[str], None] = log.error,
        *,
        default: Any = None,
    ) -> Callable:
    """
    Return a decorator that logs (with the given log function) the given
    message if the decorated function raises an uncaught TMDbException.

    Args:
        message: Message to log upon uncaught exception.
        log_func: Log function to call upon uncaught exception.
        default: (Keyword) Value to return if decorated function raises
            an uncaught exception.

    Returns:
        Wrapped decorator that returns a wrapped callable.
    """

    def decorator(function: Callable) -> Callable:
        def inner(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except TMDbException as e:
                log_func(message)
                log.exception(f'TMDbException from {function.__name__}'
                                f'({args}, {kwargs})', e)
                return default
        return inner
    return decorator


class TMDbInterface(EpisodeDataSource, WebInterface):
    """
    This class defines an interface to TheMovieDatabase (TMDb). Once
    initialized  with a valid API key, the primary purpose of this class
    is to gather images for title cards, logos for summaries, or
    translations for titles.
    """

    """Default for how many failed requests lead to a blacklisted entry"""
    BLACKLIST_THRESHOLD = 5

    """Series ID's that can be set by TMDb"""
    SERIES_IDS = ('imdb_id', 'tmdb_id', 'tvdb_id', 'tvrage_id')

    """Language codes"""
    LANGUAGES = {
        'ar': 'Arabic',
        'bg': 'Bulgarian',
        'ca': 'Catalan',
        'cs': 'Czech',
        'da': 'Danish',
        'de': 'German',
        'el': 'Greek',
        'en': 'English',
        'es': 'Spanish',
        'fa': 'Persian',
        'fr': 'French',
        'he': 'Hebrew',
        'hu': 'Hungarian',
        'id': 'Indonesian',
        'it': 'Italian',
        'ja': 'Japanese',
        'ko': 'Korean',
        'my': 'Burmese',
        'pl': 'Polish',
        'pt': 'Portuguese',
        'ro': 'Romanian',
        'ru': 'Russian',
        'sk': 'Slovak',
        'sr': 'Serbian',
        'th': 'Thai',
        'tr': 'Turkish',
        'uk': 'Ukrainian',
        'vi': 'Vietnamese',
        'zh': 'Mandarin',
    }
    LANGUAGE_CODES = tuple(LANGUAGES.keys())

    """Generic translated episode format strings for each language code"""
    GENERIC_TITLE_FORMATS = {
        'ar': r'الحلقة {number}',
        'cs': r'{number}. epizoda',
        'de': r'Episode {number}',
        'en': r'Episode {number}',
        'es': r'Episodio {number}',
        'fr': r'Épisode {number}',
        'he': r'פרק {number}',
        'hu': r'{number}. epizód',
        'id': r'Episode {number}',
        'it': r'Episodio {number}',
        'ja': r'第{number}話',
        'ko': r'에피소드 {number}',
        'pl': r'Odcinek {number}',
        'pt': r'Episódio {number}',
        'ro': r'Episodul {number}',
        'ru': r'Эпизод {number}',
        'sk': r'Epizóda {number}',
        'th': r'Episode {number}',
        'tr': r'{number}. Bölüm',
        'uk': r'Серія {number}',
        'vi': r'Episode {number}',
        'zh': r'第 {number} 集',
    }

    """Filename for where to store blacklisted entries"""
    __BLACKLIST_DB = 'tmdb_blacklist.json'


    def __init__(self, api_key: str) -> None:
        """
        Construct a new instance of an interface to TMDb.

        Args:
            api_key: The API key to communicate with TMDb.
        """

        super().__init__('TMDb')

        # Store global objects
        self.preferences = global_objects.pp
        self.info_set = global_objects.info_set

        # Create/read blacklist database
        self.__blacklist = PersistentDatabase(self.__BLACKLIST_DB)

        # Create API object, validate key
        try:
            self.api = TMDbAPIs(api_key, self.session)
        except Unauthorized:
            log.critical(f'TMDb API key "{api_key}" is invalid')
            sys_exit(1)


    def __repr__(self) -> str:
        """Returns an unambiguous string representation of the object."""

        return f'<TMDbInterface {self.api=}>'


    def __get_condition(self,
            query_type: str,
            series_info: SeriesInfo,
            episode_info: Optional[EpisodeInfo] = None
        ) -> Query:
        """
        Get the tinydb query condition for the given query.

        Args:
            query_type: The type of request being updated.
            series_info: SeriesInfo for the request.
            episode_info: EpisodeInfo for the request.

        Returns:
            The condition that matches the given query type, series, and
            Episode season + episode number and episode.
        """

        # Logo and backdrop queries don't use episode index
        if query_type in ('logo', 'backdrop'):
            return (
                (where('query') == query_type) &
                (where('series') == series_info.full_name)
            )

        # Query by series name and episode index
        return (
            (where('query') == query_type) &
            (where('series') == series_info.full_name) &
            (where('season') == episode_info.season_number) &
            (where('episode') == episode_info.episode_number)
        )


    def __update_blacklist(self,
            series_info: SeriesInfo,
            episode_info: Optional[EpisodeInfo],
            query_type: str) -> None:
        """
        Adds the given request to the blacklist; indicating that this
        exact request shouldn't be queried to TMDb for another day.
        Write the updated blacklist to file.

        Args:
            series_info: SeriesInfo for the request.
            episode_info: EpisodeInfo for the request.
            query_type: The type of request being updated.
        """

        # Get the entry for this request
        condition = self.__get_condition(query_type, series_info, episode_info)
        entry = self.__blacklist.get(condition)

        # If previously indexed and next has passed, increase count and set next
        later = (datetime.now() + timedelta(hours=12)).timestamp()

        # If this entry exists, check that next has passed
        if entry is not None:
            if datetime.now().timestamp() >= entry['next']:
                self.__blacklist.upsert(
                    {'failures': entry['failures']+1, 'next': later},
                    condition
                )
        else:
            if query_type in ('logo', 'backdrop'):
                self.__blacklist.upsert({
                    'query': query_type,
                    'series': series_info.full_name,
                    'failures': 1,
                    'next': later,
                }, condition)
            else:
                self.__blacklist.upsert({
                    'query': query_type,
                    'series': series_info.full_name,
                    'season': episode_info.season_number,
                    'episode': episode_info.episode_number,
                    'failures': 1,
                    'next': later,
                }, condition)


    def __is_blacklisted(self,
            series_info: SeriesInfo,
            episode_info: EpisodeInfo,
            query_type: str) -> bool:
        """
        Determines if the specified entry is in the blacklist (e.g.
        should not bother querying TMDb.

        Args:
            series_info: SeriesInfo for the entry.
            episode_info: EpisodeInfo for the entry.
            query_type: The type of request being checked.

        Returns:
            True if the entry is blacklisted, False otherwise.
        """

        # Get the blacklist entry for this request
        entry = self.__blacklist.get(
            self.__get_condition(query_type, series_info, episode_info)
        )

        # If request DNE, not blacklisted
        if entry is None:
            return False

        # If too many failures, blacklisted
        if entry['failures'] > self.preferences.tmdb_retry_count:
            return True

        # If next hasn't passed, treat as temporary blacklist
        return datetime.now().timestamp() < entry['next']


    def is_permanently_blacklisted(self,
            series_info: SeriesInfo,
            episode_info: EpisodeInfo,
            query_type: str = 'image') -> bool:
        """
        Determines if permanently blacklisted.

        Args:
            series_info: The series information
            episode_info: The episode information

        Returns:
            True if permanently blacklisted, False otherwise.
        """

        # Get the blacklist entry for this request
        entry = self.__blacklist.get(
            self.__get_condition(query_type, series_info, episode_info)
        )

        # If request hasn't been blacklisted, not blacklisted
        if entry is None:
            return False

        # If too many failures, blacklisted
        return entry['failures'] > self.preferences.tmdb_retry_count


    @catch_and_log('Error setting series ID')
    def set_series_ids(self,
            library_name: Optional[str],
            series_info: SeriesInfo,
        ) -> None:
        """
        Set all possible series ID's for the given SeriesInfo object.

        Args:
            library_name: Unused argument.
            series_info: SeriesInfo object to update.
        """

        # If all possible ID's are defined
        if series_info.has_ids(*self.SERIES_IDS):
            return None

        # Try and find by TMDb ID first
        found = False
        if not found and series_info.has_id('tmdb_id'):
            try:
                results = [self.api.tv_show(series_info.tmdb_id)]
                found = True
            except NotFound:
                pass

        # Find by TVDb ID
        if not found and series_info.has_id('tvdb_id'):
            try:
                results = self.api.find_by_id(
                    tvdb_id=series_info.tvdb_id
                ).tv_results
                found = len(results) > 0
            except NotFound:
                pass

        # Find by IMDb ID
        if not found and series_info.has_id('imdb_id'):
            try:
                results = self.api.find_by_id(
                    imdb_id=series_info.imdb_id
                ).tv_results
                found = len(results) > 0
            except NotFound:
                pass

        # Find by TVRage ID
        if not found and series_info.has_id('tvrage_id'):
            try:
                results = self.api.find_by_id(
                    tvrage_id=series_info.tvrage_id
                ).tv_results
                found = len(results) > 0
            except NotFound:
                pass

        # Find by series name + year
        if not found:
            try:
                # Search by name+year, and exclude adult content
                results = self.api.tv_search(series_info.name, False,
                                             series_info.year)
                found = results.total_results > 0
            except NotFound:
                pass

        # If found, update TMDb, IMDb, TVDb, and TVRage ID's
        if found:
            result = results[0]
            self.info_set.set_tmdb_id(series_info, int(result.id))
            if (imdb_id := result.imdb_id):
                self.info_set.set_imdb_id(series_info, imdb_id)
            if (tvdb_id := result.tvdb_id):
                self.info_set.set_tvdb_id(series_info, tvdb_id)
            if (tvrage_id := result.tvrage_id):
                self.info_set.set_tvrage_id(series_info, tvrage_id)
        else:
            log.warning(f'Series "{series_info}" not found on TMDb')

        return None


    @catch_and_log('Error getting all episodes', default=[])
    def get_all_episodes(self,
            library_name: str,
            series_info: SeriesInfo,
            episode_infos: Optional[list[EpisodeInfo]] = None,
        ) -> list[EpisodeInfo]:
        """
        Gets all episode info for the given series. Only episodes that
        have already aired are returned.

        Args:
            library_name: Unused argument.
            series_info: Series to get the episodes of.
            episode_infos: Unused argument.

        Returns:
            List of EpisodeInfo objects for this series.
        """

        # Cannot query TMDb if no series TMDb ID
        if series_info.tmdb_id is None:
            log.error(f'Cannot source episodes from TMDb for {series_info}')
            return []

        # Get all seasons on TMDb
        try:
            seasons = self.api.tv_show(series_info.tmdb_id).seasons
        except NotFound:
            log.error(f'Cannot source episodes from TMDb for {series_info}')
            return []

        # Go through each season, getting episodes from each
        all_episodes = []
        for season in seasons:
            # Load episodes, now iterate through them
            season.reload()
            for episode in season.episodes:
                # Skip episodes until they've aired
                if (episode.air_date is not None
                    and episode.air_date > datetime.now()):
                    continue

                # Create either a new EpisodeInfo or get from the MediaInfoSet
                try:
                    episode.reload()
                except NotFound:
                    log.error(f'TMDb error - skipping {episode}')
                    continue

                # Create new EpisodeInfo via global MediaInfoSet object
                if episode_infos is None:
                    episode_info = self.info_set.get_episode_info(
                        series_info,
                        episode.name,
                        season.season_number,
                        episode.episode_number,
                        tmdb_id=episode.id,
                        tvdb_id=episode.tvdb_id if episode.tvdb_id != 0 else None,
                        imdb_id=episode.imdb_id,
                        airdate=episode.air_date,
                        title_match=True,
                        queried_tmdb=True,
                    )
                    all_episodes.append(episode_info)
                else:
                    tmp_ei = (season.season_number, episode.episode_number)
                    for episode_info in episode_infos:
                        # Index match, update ID's
                        if episode_info == tmp_ei:
                            if episode.tvdb_id == 0:
                                tvdb_id = None
                            else:
                                tvdb_id = episode.tvdb_id
                            episode_info.set_imdb_id(episode.imdb_id)
                            episode_info.set_tmdb_id(episode.id)
                            episode_info.set_tvdb_id(tvdb_id)
                            all_episodes.append(episode_info)
                            break

        return all_episodes


    def __find_episode(self,
            series_info: SeriesInfo,
            episode_info: EpisodeInfo,
            title_match: bool = True
        ) -> Optional[TMDbEpisode]:
        """
        Finds the episode index for the given entry. Searching is done
        in the following priority:

          1. Episode TVDb ID
          2. Episode IMDb ID (as episode)
          3. Episode TVRage ID
          4. Episode IMDb ID (as movie)
          5. Episode title as movie (if no series TMDb ID is present)
          6. Series TMDb ID and season+episode index with title match
          7. Series TMDb ID and season+absolute episode index with title match
          8. Series TMDb ID and title match on any episode

        Args:
            series_info: The series information.
            episode_info: The episode information.
            title_match: Whether to require the title within
                episode_info to match the title on TMDb.

        Returns:
            Dictionary of the index for the given entry. This dictionary
            has keys 'season' and 'episode'. None if returned if the
            entry cannot be found.
        """

        # Query with TVDb ID first
        if episode_info.has_id('tvdb_id'):
            try:
                results = self.api.find_by_id(tvdb_id=episode_info.tvdb_id)
                (episode := results.tv_episode_results[0]).reload()
                return episode
            except (NotFound, IndexError, TMDbException):
                pass

        # Query with IMDb ID
        if episode_info.has_id('imdb_id'):
            try:
                results = self.api.find_by_id(imdb_id=episode_info.imdb_id)
                # Check for an episode, then check for a movie
                if len(results.tv_episode_results) > 0:
                    (episode := results.tv_episode_results[0]).reload()
                elif len(results.movie_results) > 0:
                    (episode := results.movie_results[0]).reload()
                else:
                    raise NotFound
                return episode
            except (NotFound, IndexError, TMDbException):
                pass

        # Query with TVRage ID
        if episode_info.has_id('tvrage_id'):
            try:
                results = self.api.find_by_id(tvrage_id=episode_info.tvrage_id)
                # Check for an episode, then check for a movie
                if len(results.tv_episode_results) > 0:
                    (episode := results.tv_episode_results[0]).reload()
                elif len(results.movie_results) > 0:
                    (episode := results.movie_results[0]).reload()
                else:
                    raise NotFound
                return episode
            except (NotFound, IndexError, TMDbException):
                pass

        # Search for movie with this episode title
        def _find_episode_as_movie(episode_info):
            try:
                # Search for movies with this title
                results = self.api.movie_search(episode_info.title.full_title)
                (movie := results[0]).reload()

                # Check for TMDb ID match
                id_match = (episode_info.has_id('tmdb_id')
                            and episode_info.tmdb_id == movie.id)

                # Check for title match
                title_match = episode_info.title.matches(
                    movie.title,*(alt.title for alt in movie.alternative_titles)
                )

                # Verify release date match +/- 1 day
                release_date = movie.release_date
                release_date_match = (
                    episode_info.airdate is not None
                    and release_date is not None
                    and episode_info.airdate - timedelta(days=1) <= release_date
                    and episode_info.airdate + timedelta(days=1) >= release_date
                )

                assert id_match or (title_match and release_date_match)

                # Actual match, return "movie"
                log.info(f'Matched {episode_info} of "{series_info}" to TMDb '
                         f'Movie {movie}')
                return movie
            except (NotFound, IndexError, AssertionError, TMDbException):
                return None

        # If series TMDb ID is not present, try as movie, no other attempts
        if not series_info.has_id('tmdb_id'):
            return _find_episode_as_movie(episode_info)

        # Verify series ID is valid
        try:
            series = self.api.tv_show(series_info.tmdb_id)
        except (NotFound, TMDbException):
            return None

        def _match_by_index(episode_info, season_number, episode_number):
            # Find episode with series TMDb ID and given index
            try:
                episode = self.api.tv_episode(series_info.tmdb_id,
                                              season_number, episode_number)
                episode.reload()
            except (NotFound, TMDbException):
                return None

            # If TMDb ID matches, or title matches
            id_match = (episode_info.has_id('tmdb_id')
                        and episode_info.tmdb_id == episode.id)
            does_match = (not title_match or (title_match and
                          episode_info.title.matches(episode.name)))
            return episode if id_match or does_match else None

        # Try and match by index
        indices = episode_info.season_number, episode_info.episode_number
        if (episode := _match_by_index(episode_info, *indices)) is not None:
            episode.reload()
            return episode

        # Match by absolute number
        if episode_info.abs_number is not None:
            # Try for this season
            indices = episode_info.season_number, episode_info.abs_number
            if (ep := _match_by_index(episode_info, *indices)) is not None:
                ep.reload()
                return ep

            # Try for all seasons
            for season in series.seasons:
                indices = season.season_number, episode_info.abs_number
                if (ep := _match_by_index(episode_info, *indices)) is not None:
                    ep.reload()
                    return ep

        # If title match is disabled, cannot identify
        if not title_match:
            return _find_episode_as_movie(episode_info)

        # Try every episode
        for season in series.seasons:
            season.reload()
            for episode in season.episodes:
                if ((episode_info.has_id('tmdb_id') and
                    episode_info.tmdb_id == episode.id)
                    or episode_info.title.matches(episode.name)):
                    episode.reload()
                    return episode

        return _find_episode_as_movie(episode_info)


    @catch_and_log('Error setting episode IDs')
    def set_episode_ids(self,
            library_name: Optional[str],
            series_info: SeriesInfo,
            episode_infos: list[EpisodeInfo],
            *,
            inplace: bool = False,
        ) -> None:
        """Unused, as TMDb cannot provide useful ID's."""

        return None


    def __determine_best_image(self,
            images: list[TMDbStill],
            *,
            is_source_image: bool = True,
            skip_localized: bool = False,
        ) -> dict[str, Any]:
        """
        Determine the best image and return it's contents from within the
        database return JSON.

        Args:
            images: The results from the database. Each entry is a new image to
                be considered.
            is_source_image: (Keyword only) Whether the images being selected
                are source images or not. If True, then images must meet the
                minimum resolution requirements.
            skip_localized: (Keyword only) Whether to skip localized images.

        Args:
            The "best" image for title card creation. This is determined using
            the images dimensions. Priority given to largest image. None if
            there are no valid images.
        """

        # Pick the best image based on image dimensions, and then vote average
        best_image = {'index': 0, 'pixels': 0, 'score': 0}
        valid_image = False
        for index, image in enumerate(images):
            # Get image dimensions
            width, height = image.width, image.height

            # If source image selection, check dimensions and localization
            if is_source_image:
                if not self.preferences.meets_minimum_resolution(width, height):
                    continue
                if skip_localized and image.iso_639_1 is not None:
                    continue

            # If the image has valid dimensions,get pixel count and vote average
            valid_image = True
            pixels = height * width
            score = image.vote_average

            # Priority 1 is image size, priority 2 is vote average/score
            if (pixels > best_image['pixels'] or (pixels == best_image['pixels']
                and score > best_image['score'])):
                best_image = {'index': index, 'pixels': pixels, 'score': score}

        return images[best_image['index']] if valid_image else None


    @catch_and_log('Error getting source image', default=None)
    def get_source_image(self,
            series_info: SeriesInfo,
            episode_info: EpisodeInfo, *,
            title_match: bool = True,
            skip_localized_images: bool = False,
        ) -> Optional[str]:
        """
        Get the best source image for the requested entry. The URL of
        this image is returned.

        Args:
            series_info: SeriesInfo for this entry.
            episode_info: EpisodeInfo for this entry.
            title_match:  (Keyword only) Whether to require the episode
                title to match when querying TMDb.
            skip_localized_images: (Keyword only) Whether to skip images
                with a non-null language code - i.e. skipping localized
                images.

        Returns:
            URL to the 'best' source image for the requested entry. None
            if no images are available.
        """

        # Don't query the database if this episode is in the blacklist
        if self.__is_blacklisted(series_info, episode_info, 'image'):
            return None

        # Get Episode object for this episode
        episode = self.__find_episode(series_info, episode_info, title_match)
        if episode is None:
            log.debug(f'TMDb has no matching episode for "{series_info}" '
                      f'{episode_info}')
            self.__update_blacklist(series_info, episode_info, 'image')
            return None

        # Episode found on TMDb, get images/backdrops based on episode/movie
        if hasattr(episode, 'stills'):
            images = episode.stills
        else:
            images = episode.backdrops

        # Exit if no backdrops for this episode
        if len(images) == 0:
            log.debug(f'TMDb has no images for "{series_info}" {episode_info}')
            self.__update_blacklist(series_info, episode_info, 'image')
            return None

        # Get the best image for this Episode
        kwargs = {'is_source_image':True,'skip_localized':skip_localized_images}
        if (best_image := self.__determine_best_image(images, **kwargs)):
            return best_image.url

        log.debug(f'TMDb images for "{series_info}" {episode_info} do not meet '
                  f'dimensional requirements')
        self.__update_blacklist(series_info, episode_info, 'image')
        return None


    def __is_generic_title(self,
            title: str,
            language_code: str,
            episode_info: EpisodeInfo,
        ) -> bool:
        """
        Determine whether the given title is a generic translation of
        "Episode (x)" for the indicated language.

        Args:
            title: The translated title.
            language_code: The language code of the translation.
            episode_info: The EpisodeInfo for this title.

        Returns:
            True if the title is a generic translation, False otherwise.
        """

        # Get ISO-639-1 if combined language code was given
        code = language_code
        if '-' in language_code:
            code = language_code.split('-')[0]

        # Assume non-generic if the code isn't pre-mapped
        if not (generic := self.GENERIC_TITLE_FORMATS.get(code, None)):
            log.debug(f'Unrecognized language code "{language_code}"')
            return False

        # Format with this episode, return whether this matches the translation
        if episode_info.abs_number is not None:
            # Check against episode and absolute number
            return title in (
                generic.format(number=episode_info.episode_number),
                generic.format(number=episode_info.abs_number),
            )

        # Only check against episode number (no absolute)
        return title == generic.format(number=episode_info.episode_number)


    @catch_and_log('Error getting episode title', default=None)
    def get_episode_title(self,
            series_info: SeriesInfo,
            episode_info: EpisodeInfo,
            language_code: str = 'en-US',
        ) -> Optional[str]:
        """
        Get the episode title for the given entry for the given language.

        Args:
            series_info: SeriesInfo for the entry.
            episode_info: EpisodeInfo for the entry.
            language_code: The language code for the desired title.

        Args:
            The episode title, None if the entry does not exist.
        """

        # Don't query the database if this episode is in the blacklist
        if self.__is_blacklisted(series_info, episode_info, 'title'):
            return None

        # Get episode
        episode = self.__find_episode(series_info, episode_info)
        if episode is None:
            self.__update_blacklist(series_info, episode_info, 'title')
            return None

        # Look for this translation
        for translation in episode.translations:
            codes = (translation.iso_639_1, translation.iso_3166_1)
            combined_code = '-'.join(codes)
            if (('-' in language_code and language_code == combined_code)
                or language_code in codes):
                # If the title translation is blank (i.e. non-existent)
                if hasattr(translation, 'name'):
                    title = translation.name
                else:
                    title = translation.title
                if len(title) == 0:
                    break

                # If translation is generic, blacklist and skip
                if self.__is_generic_title(title, language_code, episode_info):
                    log.debug(f'Generic title "{title}" detected for '
                              f'{episode_info}')
                    self.__update_blacklist(series_info, episode_info, 'title')
                    return None

                return title

        return None


    @catch_and_log('Error getting series logo', default=None)
    def get_series_logo(self, series_info: SeriesInfo) -> Optional[str]:
        """
        Get the best logo for the given series.

        Args:
            series_info: Series to get the logo of.

        Returns:
            URL to the 'best' logo for the given series, and None if no
            images  are available.
        """

        # Don't query the database if this series' logo is blacklisted
        if self.__is_blacklisted(series_info, None, 'logo'):
            return None

        # Get the series for this logo, exit if series or logos DNE
        try:
            series = self.api.tv_show(series_info.tmdb_id)
        except NotFound:
            self.__update_blacklist(series_info, None, 'logo')
            return None

        # Blacklist if tthere are no logos
        if len(series.logos) == 0:
            self.__update_blacklist(series_info, None, 'logo')
            return None

        # Get the best logo
        best, best_priority = None, 999
        for logo in series.logos:
            # Skip logos with unindicated languages
            if (logo.iso_639_1
                not in self.preferences.tmdb_logo_language_priority):
                continue
            # Get relative priority of this logo's language
            priority = self.preferences.tmdb_logo_language_priority.index(
                logo.iso_639_1
            )

            # Skip this logo if the language priority is less than the current
            # best. Highest priority is index 0, so use > for lower priority
            if priority > best_priority:
                continue
            # New logo is higher priority, use always
            if priority < best_priority:
                best = logo
                best_priority = priority
            # Same priority, compare sizes
            elif priority == best_priority:
                # SVG logos are infinite size
                if logo.url.endswith('.svg') and not best.url.endswith('.svg'):
                    best = logo
                    best_priority = priority
                elif (best is None
                    or logo.width * logo.height > best.width * best.height):
                    best = logo
                    best_priority = priority

        # No valid image found, blacklist and exit
        if best is None:
            self.__update_blacklist(series_info, None, 'logo')
            return None

        return best.url


    @catch_and_log('Error setting series backdrop', default=None)
    def get_series_backdrop(self,
            series_info: SeriesInfo,
            *,
            skip_localized_images: bool = False,
        ) -> Optional[str]:
        """
        Get the best backdrop for the given series.

        Args:
            series_info: Series to get the logo of.
            skip_localized_images: Whether to skip images with a non-
                null language code - i.e. skipping localized images.

        Returns:
            URL to the 'best' backdrop for the given series, and None if
            no  images are available.
        """

        # Don't query the database if this episode is in the blacklist
        if self.__is_blacklisted(series_info, None, 'backdrop'):
            return None

        # Get the series for this backdrop, exit if series or backdrop DNE
        try:
            series = self.api.tv_show(series_info.tmdb_id)
        except NotFound:
            self.__update_blacklist(series_info, None, 'backdrop')
            return None

        # Blacklist if there are no backdrops
        if len(series.backdrops) == 0:
            self.__update_blacklist(series_info, None, 'backdrop')
            return None

        # Find and return best image
        best_image = self.__determine_best_image(
            series.backdrops,
            is_source_image=True,
            skip_localized=skip_localized_images,
        )

        if best_image:
            return best_image.url

        self.__update_blacklist(series_info, None, 'backdrop')
        return None


    def manually_download_season(self,
            title: str,
            year: int,
            season_number: int,
            episode_range: Iterable[int],
            directory: Path,
        ) -> None:
        """
        Download episodes 1-episode_count of the requested season for the given
        show. They will be named as s{season}e{episode}.jpg.

        Args:
            title: The title of the requested show.
            year: The year of the requested show.
            season_number: Which season to download.
            episode_range: Episode numbers to download images of.
            directory: The directory to place the downloaded images in.
        """

        # Create SeriesInfo for the series
        si = SeriesInfo(title, year)
        self.set_series_ids(None, si)

        # Go through each episode in the given range
        for episode_number in episode_range:
            ei = EpisodeInfo('', season_number, episode_number)
            image_url = self.get_source_image(si, ei, title_match=False)

            # If a valid URL was returned, download it
            if image_url is not None:
                filename = f's{season_number}e{episode_number}.jpg'
                if self.download_image(image_url, directory / filename):
                    log.debug(f'Downloaded {(directory / filename).resolve()}')


    @staticmethod
    def unblacklist(series_info: SeriesInfo) -> None:
        """Remove all blacklist entries for the given series."""

        blacklist = PersistentDatabase(TMDbInterface.__BLACKLIST_DB)
        removed = blacklist.remove(where('series') == series_info.full_name)
        log.info(f'Unblacklisted {len(removed)} queries')


    @staticmethod
    def delete_blacklist(database_directory: Path) -> None:
        """Delete the blacklist file referenced by this class."""

        database = database_directory / TMDbInterface.__BLACKLIST_DB
        database.unlink(missing_ok=True)
        log.info(f'Deleted blacklist file "{database.resolve()}"')
